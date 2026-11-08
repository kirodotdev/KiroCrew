"""The cloud wizard's terminal UI: prompts, pickers, output helpers and spinner.

``test_cloud_ui.py`` pins the cursor-key mapping, the cursor-menu guard and the
Ctrl-C re-raise. This file pins the rest of :mod:`kiro_crew.cloud.ui`:

* ``prompt`` / ``confirm`` fall back to their default on empty input and on EOF,
  which is what an unattended ``curl | bash`` install sees;
* ``choose`` uses the numbered fallback off a TTY, and that fallback re-asks on a
  non-number or an out-of-range number;
* the cursor picker enters cbreak mode, redraws in place, and restores the terminal
  attributes however it exits;
* ``_read_key`` reads a whole arrow-key escape sequence;
* ``Steps``, the one-line helpers and ``Spinner`` in both modes.

Colors are blanked for every test, so assertions do not depend on whether the
developer's stdout is a TTY. ``input``, stdin and ``termios``/``tty`` are faked.
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable

import pytest

from kiro_crew.cloud import ui

_COLORS = ("BOLD", "DIM", "RESET", "RED", "GREEN", "YELLOW", "BLUE", "MAGENTA", "CYAN")


@pytest.fixture(autouse=True)
def _plain_text(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _COLORS:
        _floor_monkeypatch.setattr(ui, name, "")
    _floor_monkeypatch.setattr(ui, "_ENABLE_COLOR", False)


def _answers(monkeypatch: pytest.MonkeyPatch, replies: Iterable[str | type[BaseException]]):
    """Script ``input()``: each reply is returned, or raised if it is an exception class."""
    queue = list(replies)
    asked: list[str] = []

    def _input(text: str = "") -> str:
        asked.append(text)
        reply = queue.pop(0)
        if isinstance(reply, type) and issubclass(reply, BaseException):
            raise reply()
        return reply

    monkeypatch.setattr("builtins.input", _input)
    return asked


class TestPrompt:
    def test_the_answer_is_stripped(self, monkeypatch) -> None:
        asked = _answers(monkeypatch, ["  us-east-1  "])
        assert ui.prompt("Region", "us-west-2") == "us-east-1"
        assert asked == ["  ? Region [us-west-2]: "]

    def test_an_empty_answer_takes_the_default(self, monkeypatch) -> None:
        _answers(monkeypatch, [""])
        assert ui.prompt("Region", "us-west-2") == "us-west-2"

    def test_no_default_is_shown_when_there_is_none(self, monkeypatch) -> None:
        asked = _answers(monkeypatch, ["x"])
        ui.prompt("Name")
        assert asked == ["  ? Name: "]

    def test_eof_takes_the_default(self, monkeypatch, capsys) -> None:
        _answers(monkeypatch, [EOFError])
        assert ui.prompt("Region", "us-west-2") == "us-west-2"
        assert capsys.readouterr().out == "\n"  # ends the dangling prompt line


class TestConfirm:
    @pytest.mark.parametrize(
        ("reply", "default", "expected"),
        [
            ("y", False, True),
            ("YES", False, True),
            ("n", True, False),
            ("nope", True, False),  # anything that is not yes is no
            ("", True, True),
            ("", False, False),
        ],
    )
    def test_answers(self, monkeypatch, reply, default, expected) -> None:
        _answers(monkeypatch, [reply])
        assert ui.confirm("Go?", default=default) is expected

    @pytest.mark.parametrize(("default", "hint"), [(True, "[Y/n]"), (False, "[y/N]")])
    def test_the_default_is_shown_capitalized(self, monkeypatch, default, hint) -> None:
        asked = _answers(monkeypatch, [""])
        ui.confirm("Go?", default=default)
        assert asked == [f"  ? Go? {hint} "]

    @pytest.mark.parametrize("default", [True, False])
    def test_eof_takes_the_default(self, monkeypatch, default) -> None:
        _answers(monkeypatch, [EOFError])
        assert ui.confirm("Go?", default=default) is default


OPTIONS = [("Small", "2 vCPU"), ("Medium", ""), ("Large", "8 vCPU")]


class TestNumberedPicker:
    @pytest.fixture(autouse=True)
    def _no_tty(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(ui, "_supports_cursor_menu", lambda: False)

    def test_enter_takes_the_recommended_option(self, monkeypatch, capsys) -> None:
        asked = _answers(monkeypatch, [""])
        assert ui.choose("Size", OPTIONS, default_index=1) == 1
        assert asked == ["  ? Choose 1-3 [2]: "]
        assert capsys.readouterr().out == (
            "  Size\n"
            "    ○ Small\n"
            "       2 vCPU\n"
            "    ◉ Medium (recommended)\n"
            "    ○ Large\n"
            "       8 vCPU\n"
        )

    def test_a_bad_answer_is_asked_again(self, monkeypatch, capsys) -> None:
        asked = _answers(monkeypatch, ["two", "4", "0", "3"])
        assert ui.choose("Size", OPTIONS) == 2
        assert len(asked) == 4
        out = capsys.readouterr().out
        assert out.count("⚠ Enter a number.\n") == 1
        assert out.count("⚠ Enter a number between 1 and 3.\n") == 2

    def test_eof_takes_the_recommended_option(self, monkeypatch) -> None:
        _answers(monkeypatch, [EOFError])
        assert ui.choose("Size", OPTIONS, default_index=2) == 2


class _Stdin:
    """A fake stdin: a fixed fileno and a scripted keystroke buffer."""

    def __init__(self, keys: str) -> None:
        self._buf = keys

    def fileno(self) -> int:
        return 7

    def read(self, n: int) -> str:
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


@pytest.mark.skipif(os.name != "posix", reason="the cursor picker uses termios/tty")
class TestCursorPicker:
    @pytest.fixture()
    def terminal(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
        import termios
        import tty

        calls: list[tuple] = []

        def _get(fd: int) -> list[str]:
            calls.append(("get", fd))
            return ["old"]

        monkeypatch.setattr(termios, "tcgetattr", _get)
        monkeypatch.setattr(
            termios, "tcsetattr", lambda fd, when, attrs: calls.append(("set", fd, attrs))
        )
        monkeypatch.setattr(tty, "setcbreak", lambda fd: calls.append(("cbreak", fd)))
        monkeypatch.setattr(ui, "_supports_cursor_menu", lambda: True)
        return calls

    def test_arrows_move_and_enter_selects(self, monkeypatch, capsys, terminal) -> None:
        monkeypatch.setattr(ui.sys, "stdin", _Stdin("\x1b[B\x1b[B\r"))
        assert ui.choose("Size", OPTIONS) == 2
        assert terminal == [("get", 7), ("cbreak", 7), ("set", 7, ["old"])]
        out = capsys.readouterr().out
        # Drawn once and redrawn twice, each redraw moving up over the 7 lines drawn.
        assert out.count("\033[7A") == 2
        assert out.count("Use ↑/↓ and Enter, or press 1-3.") == 3

    def test_a_digit_selects_without_enter(self, monkeypatch, terminal) -> None:
        monkeypatch.setattr(ui.sys, "stdin", _Stdin("2"))
        assert ui.choose("Size", OPTIONS) == 1

    def test_the_terminal_is_restored_when_reading_fails(self, monkeypatch, terminal) -> None:
        def _boom() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(ui.sys, "stdin", _Stdin(""))
        monkeypatch.setattr(ui, "_read_key", _boom)
        with pytest.raises(KeyboardInterrupt):
            ui.choose("Size", OPTIONS)
        assert terminal[-1] == ("set", 7, ["old"])

    def test_an_out_of_range_default_is_clamped(self, monkeypatch, terminal) -> None:
        monkeypatch.setattr(ui.sys, "stdin", _Stdin("\r"))
        assert ui.choose("Size", OPTIONS, default_index=9) == 2


class TestKeysAndLines:
    def test_an_escape_sequence_is_read_whole(self, monkeypatch) -> None:
        monkeypatch.setattr(ui.sys, "stdin", _Stdin("\x1b[Ak"))
        assert ui._read_key() == "\x1b[A"
        assert ui._read_key() == "k"

    def test_choice_lines_mark_only_the_selection(self) -> None:
        lines = ui._choice_lines("Size", OPTIONS, 0)
        assert lines == [
            "  Size",
            "    Use ↑/↓ and Enter, or press 1-3.",
            "    ◉ Small (recommended)",
            "       2 vCPU",
            "    ○ Medium",
            "    ○ Large",
            "       8 vCPU",
        ]

    def test_an_empty_menu_finishes_at_once(self) -> None:
        assert ui._apply_choice_key("j", 0, 0) == (0, True)

    def test_an_unknown_key_changes_nothing(self) -> None:
        assert ui._apply_choice_key("x", 1, 3) == (1, False)
        assert ui._apply_choice_key("9", 1, 3) == (1, False)  # a digit past the end


class TestOutput:
    def test_steps_are_numbered_out_of_the_total(self, capsys) -> None:
        steps = ui.Steps(2)
        steps.step("Pick a region")
        steps.step("Deploy")
        out = capsys.readouterr().out
        assert "  [1/2] Pick a region\n" in out
        assert "  [2/2] Deploy\n" in out

    def test_one_line_helpers(self, capsys) -> None:
        ui.info("i")
        ui.ok("o")
        ui.warn("w")
        ui.fail("f")
        ui.detail("d")
        ui.note("n")
        assert capsys.readouterr().out == "  → i\n  ✓ o\n  ⚠ w\n  ✗ f\n    d\n  n\n"


class TestSpinner:
    def test_off_a_tty_it_prints_each_message_as_a_line(self, capsys) -> None:
        with ui.Spinner("Deploying") as s:
            s.update("Waiting for the stack")
        assert s._thread is None
        assert capsys.readouterr().out == "  → Deploying\n  → Waiting for the stack\n"

    def test_on_a_tty_it_spins_on_a_thread_that_stops_on_exit(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(ui, "_ENABLE_COLOR", True)
        buf = io.StringIO()
        monkeypatch.setattr(ui.sys, "stdout", buf)
        with ui.Spinner("Deploying") as s:
            assert s._thread is not None and s._thread.daemon
            s.update("Waiting")  # updates the line in place; prints nothing new
        assert s._thread is not None
        assert not s._thread.is_alive()
        assert s.msg == "Waiting"
        out = buf.getvalue()
        assert "→" not in out  # never falls back to the plain-text line
        # The last write clears the spinner line.
        assert out.endswith("\r" + " " * (len("Waiting") + 6) + "\r")

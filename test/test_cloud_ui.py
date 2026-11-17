"""Unit tests for cloud terminal UI helpers."""

from __future__ import annotations

import io

import pytest

from kiro_crew.cloud import ui


class TestChoiceKeys:
    def test_down_wraps_and_enter_selects(self):
        selected, done, _ = ui._apply_choice_key("\x1b[B", 1, 2)
        assert selected == 0
        assert done is False
        selected, done, _ = ui._apply_choice_key("\n", selected, 2)
        assert selected == 0
        assert done is True

    def test_up_wraps(self):
        selected, done, _ = ui._apply_choice_key("\x1b[A", 0, 3)
        assert selected == 2
        assert done is False

    def test_digit_selects(self):
        selected, done, _ = ui._apply_choice_key("2", 0, 3)
        assert selected == 1
        assert done is True


class TestCursorMenuGuard:
    def test_requires_posix(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        assert ui._supports_cursor_menu() is False

    def test_requires_tty(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.setattr("sys.stdin", type("F", (), {"isatty": lambda self: False})())
        assert ui._supports_cursor_menu() is False


class TestInterrupts:
    def test_prompt_reraises_keyboard_interrupt(self, monkeypatch):
        def raise_interrupt(_prompt):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_interrupt)
        with pytest.raises(KeyboardInterrupt):
            ui.prompt("name", "default")

    def test_confirm_reraises_keyboard_interrupt(self, monkeypatch):
        def raise_interrupt(_prompt):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", raise_interrupt)
        with pytest.raises(KeyboardInterrupt):
            ui.confirm("continue?", default=True)


class TestPipedOutput:
    @pytest.mark.parametrize(
        "helper",
        [ui.info, ui.ok, ui.warn, ui.fail, ui.detail, ui.note, ui.Steps(6).step],
    )
    def test_each_line_reaches_a_block_buffered_stdout_at_once(self, monkeypatch, helper):
        raw = io.BytesIO()
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw, encoding="utf-8"))
        helper("still working")
        assert b"still working" in raw.getvalue()

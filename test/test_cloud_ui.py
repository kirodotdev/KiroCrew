"""Unit tests for cloud terminal UI helpers."""

from __future__ import annotations

import pytest

from kiro_crew.cloud import ui


class TestChoiceKeys:
    def test_down_wraps_and_enter_selects(self):
        selected, done = ui._apply_choice_key("\x1b[B", 1, 2)
        assert selected == 0
        assert done is False
        selected, done = ui._apply_choice_key("\n", selected, 2)
        assert selected == 0
        assert done is True

    def test_up_wraps(self):
        selected, done = ui._apply_choice_key("\x1b[A", 0, 3)
        assert selected == 2
        assert done is False

    def test_digit_selects(self):
        selected, done = ui._apply_choice_key("2", 0, 3)
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

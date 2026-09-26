"""Does ``WindowsPty.exitstatus()`` hand back what the binding gave it?

Covers the wrapper's guard and coercion branches, which a real binding (always a
plain ``int``) never takes. ``object.__new__`` bypasses the Windows-only
``__init__``, so this runs on every platform.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.conpty import WindowsPty


class _Binding:
    """Stand-in for ``PtyProcess`` exposing a chosen ``exitstatus`` value."""

    def __init__(self, status: object) -> None:
        self.exitstatus = status


class _RaisingBinding:
    """A binding whose ``exitstatus`` raises something other than AttributeError."""

    @property
    def exitstatus(self) -> int:
        raise OSError("the handle is gone")


def _wrapper_over(binding: object) -> WindowsPty:
    """A ``WindowsPty`` around *binding*, skipping the winpty-importing init."""
    pty = object.__new__(WindowsPty)
    pty._p = binding  # type: ignore[attr-defined]
    return pty


class TestExitStatusNormalisation:
    def test_a_reported_code_is_handed_back(self) -> None:
        assert _wrapper_over(_Binding(0)).exitstatus() == 0
        assert _wrapper_over(_Binding(3)).exitstatus() == 3

    def test_an_unresolved_status_stays_none(self) -> None:
        """A live child, and a binding without the property, both answer None."""
        assert _wrapper_over(_Binding(None)).exitstatus() is None
        assert _wrapper_over(object()).exitstatus() is None

    def test_an_int_like_status_is_coerced(self) -> None:
        """The value is whatever the native layer returned, so coerce it."""
        assert _wrapper_over(_Binding("3")).exitstatus() == 3

    def test_a_non_numeric_status_is_reported_as_unresolved(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Better an unknown status than a made-up code or a raised exception."""
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.conpty"):
            assert _wrapper_over(_Binding("not a code")).exitstatus() is None
        assert "not an integer" in caplog.text

    def test_a_raising_binding_is_contained(self) -> None:
        """The reap path treats None as unresolved; it cannot take an exception."""
        assert _wrapper_over(_RaisingBinding()).exitstatus() is None

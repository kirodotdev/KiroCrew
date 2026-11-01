"""Explicit, trusted built-in Mode plug-in registration.

This list is deliberately static. User Policy Packs remain JSON-only: they
never cause arbitrary code to be imported or executed by Mode discovery.
"""
from __future__ import annotations

from .contracts import BuiltinMode
from .tiga import MODE as TIGA
from .dyna import MODE as DYNA

_BUILTINS: tuple[BuiltinMode, ...] = (TIGA, DYNA)


def builtin_modes() -> tuple[BuiltinMode, ...]:
    """Return the trusted Mode declarations in the stable product order."""
    return _BUILTINS


def builtin_mode(mode_id: str) -> BuiltinMode | None:
    key = str(mode_id or "").strip().lower()
    return next((mode for mode in _BUILTINS if mode.id == key), None)

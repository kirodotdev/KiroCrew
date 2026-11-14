"""Explicit, trusted built-in Mode plug-in registration.

This list is deliberately static. User Policy Packs remain JSON-only: they
never cause arbitrary code to be imported or executed by Mode discovery.
"""
from __future__ import annotations

from .contracts import BuiltinMode
from .tiga import MODE as TIGA
from .dyna import MODE as DYNA

_BUILTINS: tuple[BuiltinMode, ...] = (TIGA, DYNA)

# Names of planned first-party Modes must never be claimed by user policy
# files before the matching trusted implementation ships. They do not appear
# in the registry/UI until a real BuiltinMode declaration is installed.
_FUTURE_BUILTIN_IDS = frozenset({"gaia", "cosmos"})
_LEGACY_OVERRIDE_IDS = frozenset({"tiga"})


def builtin_modes() -> tuple[BuiltinMode, ...]:
    """Return the trusted Mode declarations in the stable product order."""
    return _BUILTINS


def builtin_mode(mode_id: str) -> BuiltinMode | None:
    key = str(mode_id or "").strip().lower()
    return next((mode for mode in builtin_modes() if mode.id == key), None)


def reserved_mode_ids() -> frozenset[str]:
    """One authority for active and planned first-party Mode namespaces."""
    return frozenset(mode.id for mode in builtin_modes()) | _FUTURE_BUILTIN_IDS


def allows_legacy_override(mode_id: str) -> bool:
    """The existing manual Tiga JSON override is the sole legacy exception."""
    return mode_id in _LEGACY_OVERRIDE_IDS and builtin_mode(mode_id) is not None

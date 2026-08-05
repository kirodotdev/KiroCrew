"""Shared version parsing and compatibility checks for app management."""
from __future__ import annotations


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a semver-like string into a comparable 3-element tuple.

    Strips pre-release (``-beta``) and build metadata (``+build.42``)
    before parsing so that ``"1.2.0-rc.1"`` is treated as ``(1, 2, 0)``.
    Always pads to 3 elements so ``parse_version("1.0") == parse_version("1.0.0")``.
    """
    v = v.split("-")[0].split("+")[0]  # strip pre-release / build metadata
    parts = [int(x) for x in v.split(".")[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def check_min_version(min_version: str) -> str | None:
    """Return an error string if the current KiroCrew version is too old.

    Returns ``None`` if the version is sufficient or if parsing fails.
    """
    if not min_version:
        return None
    try:
        from kiro_crew import __version__ as current
        if parse_version(current) < parse_version(min_version):
            return (
                f"App requires Kiro Crew >= {min_version}, "
                f"but current version is {current}. "
                f"Please update Kiro Crew first."
            )
    except (ValueError, AttributeError, ImportError):
        pass
    return None

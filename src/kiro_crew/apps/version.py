"""Shared version parsing and compatibility checks for app management."""

from __future__ import annotations

import re

# Leading numeric release segment: "0.4.0rc3" -> "0.4.0", "1.2.0-rc.1" -> "1.2.0",
# "1.0+build.42" -> "1.0". An optional leading "v" is tolerated ("v1.2.3").
_RELEASE_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)")


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a semver- or PEP 440-like string into a comparable 3-element tuple.

    Only the leading numeric release segment is compared: semver pre-release
    (``1.2.0-rc.1``) and build metadata (``+build.42``) are stripped, and PEP 440
    suffixes that attach WITHOUT a separator (``0.4.0rc3``, as stamped into every
    insider wheel) are stripped too -- the old ``split("-")`` approach raised
    ``ValueError`` on those, which silently disabled ``check_min_version`` on
    insider installs. Always pads to 3 elements so
    ``parse_version("1.0") == parse_version("1.0.0")``.
    """
    m = _RELEASE_RE.match(v)
    if not m:
        raise ValueError(f"unparseable version: {v!r}")
    parts = [int(x) for x in m.group(1).split(".")[:3]]
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

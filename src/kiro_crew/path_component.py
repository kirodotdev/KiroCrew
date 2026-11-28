"""Whether a stored name is one path component, with no heavy imports.

The config loader checks every ``agents`` row with this rule before any server lists
its tools, so it lives apart from :mod:`kiro_crew.members` (which pulls in the artifact
store and the hook layer). :mod:`kiro_crew.members` still serves the name.
"""

from __future__ import annotations

from pathlib import PureWindowsPath


def is_single_path_component(value: object) -> bool:
    """Return whether *value* names exactly one file or folder, never a path.

    The rule for a stored roster key or ``member_id`` that is joined into a path: not
    empty, no ``/`` or ``\\``, not ``.`` or ``..``, no NUL and no line break, and no
    drive prefix such as ``D:`` (on Windows a drive-relative name discards the parent it
    is joined to; the rule is the same on every OS). It is not the crew-id grammar, so a
    key already stored with dots, spaces or non-Latin letters stays valid.
    """
    return (
        isinstance(value, str)
        and bool(value)
        and value not in (".", "..")
        and not any(c in value for c in "/\\\x00\n\r")
        and not PureWindowsPath(value).drive
    )

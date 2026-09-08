"""Path-segment names a POSIX filesystem accepts but Windows refuses.

Mochi mints most of its path segments itself (UUID4 pack ids), but two arrive
from outside: a petdex slug the user pastes, and an overwrite pack id sent by
the client. Both are validated by a character allow-list, which is the right
barrier for traversal and is complete on POSIX -- and incomplete on Windows,
which additionally reserves a set of legacy DOS device names in every
directory, and silently strips a trailing dot or space from a segment.
Neither rule is expressible as a character class, so it lives here.

This is deliberately NOT a security barrier: the callers' own allow-lists
already reject every separator and every illegal character, and this module is
consulted after them. It exists for two narrower reasons -- so a slug like
``con`` fails with a message the user can act on instead of an OSError raised
deep inside a file write, and so ``pet.`` and ``pet`` cannot name one directory
on Windows while naming two everywhere else.
"""

from __future__ import annotations

#: Legacy DOS device names. Windows reserves each of these in EVERY directory,
#: and reserves them WITH an extension too, so ``con.json`` is as unusable as
#: ``con``. Lowercase because the comparison lowercases; the reservation itself
#: is case-insensitive.
_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

#: Windows strips these from the end of a path segment, so a name ending in one
#: resolves to a DIFFERENT name than the one requested.
_STRIPPED_TRAILING = frozenset({".", " "})


def is_windows_reserved(segment: str) -> bool:
    """Return True when ``segment`` is unusable as a path segment on Windows.

    Evaluated unconditionally rather than behind ``platform_compat.IS_WINDOWS``.
    The data directory is portable -- it can be synced or copied between hosts --
    so a name accepted on macOS must not be one that becomes unreadable when the
    same directory is later opened on Windows. Rejecting it on every platform
    keeps one store shape instead of two.
    """
    if not segment:
        return True
    if segment[-1] in _STRIPPED_TRAILING:
        return True
    stem = segment.partition(".")[0]
    return stem.lower() in _RESERVED_STEMS

"""The ``package/<qualifier>:<rel>`` skill key shape, shared by every reader of it."""

from __future__ import annotations

_PACKAGE_QUALIFIER_BYTES = 8
_HEX = frozenset("0123456789abcdef")


def split_package_key(pkg_rel: str) -> tuple[str | None, str]:
    """Split a ``package/`` key remainder into ``(qualifier, rel)``.

    Only the exact shape ``package_root_qualifier`` mints counts, so a skill
    directory whose own name contains ``:`` still resolves unqualified.
    """
    qualifier, sep, rel = pkg_rel.partition(":")
    if sep and rel and len(qualifier) == _PACKAGE_QUALIFIER_BYTES * 2 and set(qualifier) <= _HEX:
        return qualifier, rel
    return None, pkg_rel

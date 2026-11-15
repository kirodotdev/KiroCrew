"""Sensitive-path checks: the gate every gateway file surface asks first.

Both answer from the gateway's own sensitive-location list (credential homes, the
governance ceiling under the data home, any ``KIROCREW_HOME`` re-anchoring), with
its symlink and case-fold hardening. A ``True`` means refuse: never read, write,
stage or sweep the path.
"""

from __future__ import annotations

from kiro_crew import security


def is_sensitive_path(path: str) -> bool:
    """Whether *path* lies inside a location the gateway refuses to read or write.

    The decision is ``kiro_crew.security.is_sensitive_path``'s; a check that could
    not be completed answers ``True`` there, so a caller that only holds this
    boolean refuses fail-closed.
    """
    return bool(security.is_sensitive_path(path))


def contains_sensitive_path(directory: str) -> bool:
    """Whether a sensitive location lies UNDER *directory*.

    The reverse direction of :func:`is_sensitive_path`, for a bulk operation
    rooted at *directory* (a ``git add -A``, an archive, a recursive copy): a root
    that is an ancestor of a credential store would sweep it in even though the
    root itself is not sensitive.
    """
    return bool(security.path_contains_sensitive(directory))

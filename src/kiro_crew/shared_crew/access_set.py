"""The access set's read side: the owner's tailnet login and the removed people.

Admission, the turn gate and attribution read who the owner is and whether a
person was removed through these three functions. This module holds no
membership source yet, so the owner has no tailnet login and nobody is removed:
every caller keeps its single-operator answer.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_OWNER: dict[str, str] = {"login": "", "user_id": ""}
_REMOVED: set[str] = set()


def owner_login() -> str:
    """This machine's own Tailscale login, or ``""`` when not known."""
    with _LOCK:
        return _OWNER["login"]


def principal_removed(principal_id: str) -> bool:
    """Whether work charged to *principal_id* must not start: its person was removed."""
    with _LOCK:
        return bool(principal_id) and principal_id in _REMOVED


def principal_ids_for(login: str, user_ids: frozenset[str]) -> frozenset[str]:
    """Every principal id a person may have worked under.

    A tailnet person is keyed by login on every transport, so that is one id.
    *user_ids* are aliases of the login and add none.
    """
    from kiro_crew.shared_crew.contracts import Provenance
    from kiro_crew.shared_crew.identity import tailnet_principal

    if not login:
        return frozenset()
    return frozenset({tailnet_principal(login, "", Provenance.TAILSCALE_SERVE).id})


__all__ = ["owner_login", "principal_ids_for", "principal_removed"]

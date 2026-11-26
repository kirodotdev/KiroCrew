"""What E9 compares a pinned tree with once the app's wheel copy is gone.

While a pinned app's wheel copy ships, ``register_builtin_apps`` rewrites the
builtin-owned record from it at every start: the record's ``app.json`` copy, and an
approved set of everything that copy declares (``manager.builtin_approved_grants``).
E9 (``builtin_adoption_checks.permission_refusal``) and the swap (``update_app``
under an adoption) compare the pinned tree with that record. A release that deletes
the copy ends the rewrite, so a host that never adopted keeps whatever its last
booted release wrote, which can be an older, narrower copy. The release that deletes
a copy therefore freezes the copy's E9 input into its pin row
(``builtin_adoption_pins.DELETED_COPY_KEY``, printed by
``scripts/adoption_pin.py freeze``), and with no wheel copy both comparisons read
that instead of the record: the host adopts exactly as one that booted the last
release with the copy and then upgraded.

Owning spec: ``docs/system-specs/modules/app-kit-platform.md`` § Former built-in
adoption, "A pin row with no wheel copy".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from kiro_crew.apps.builtin_adoption_pins import AdoptionPin


@dataclass(frozen=True)
class GrantBaseline:
    """The replaced side of E9 and of the adoption swap, in place of the record's copy.

    ``permissions`` is the parsed ``permissions`` the direct checks read
    (``sessionApproval``, ``memberThreads``, ``chatSlots``, ``chatLaunch``);
    ``grants`` is ``point_grants.grant_source`` of the same copy; ``approved`` is the
    approved set the held-back check starts from. On the record path each is what the
    record holds, None where it holds nothing (no readable copy, no stored set).
    """

    permissions: Any
    grants: dict[str, list[str]] | None
    approved: dict[str, list[str]] | None


def freeze_deleted_copy(app_json: dict[str, Any]) -> dict[str, Any]:
    """The ``deletedCopy`` object of a wheel copy's raw ``app.json``.

    Derived through the readers E9 and ``register_builtin_apps`` run, never by naming
    keys: the copy's own ``permissions`` object verbatim, and every grant family
    ``grant_source`` reads from the copy that declares an entry, so a family
    registered later is carried with no change here. A family left out declared
    nothing, which is how :func:`deleted_copy_baseline` reads it.
    """
    from kiro_crew.apps.point_grants import grant_source

    permissions = app_json.get("permissions")
    grants = grant_source(app_json) or {}
    return {
        "permissions": permissions if isinstance(permissions, dict) else {},
        "grants": {family: list(entries) for family, entries in grants.items() if entries},
    }


def deleted_copy_baseline(document: dict[str, Any]) -> GrantBaseline:
    """The baseline a ``deletedCopy`` object stands for: the record its copy registered.

    ``permissions`` parses as a record copy's does, and ``approved`` is what
    ``register_builtin_apps`` stores from that copy, so the adoption ends with the
    grants the wheel-rewrite path would have carried, no more.
    """
    from kiro_crew.apps.manager import builtin_approved_grants
    from kiro_crew.apps.manifest import Permissions

    raw_permissions = document.get("permissions")
    grants = {family: list(entries) for family, entries in document.get("grants", {}).items()}
    return GrantBaseline(
        permissions=(
            Permissions.from_dict(raw_permissions)
            if isinstance(raw_permissions, dict)
            else Permissions()
        ),
        grants=grants,
        approved=builtin_approved_grants(grants),
    )


def wheel_copy_ships(name: str) -> bool:
    """Whether this wheel still ships a built-in copy named *name*."""
    from kiro_crew.apps.execution import _shipped_builtin_apps

    return any(shipped == name for _root, shipped in _shipped_builtin_apps())


def no_copy_baseline(pin: AdoptionPin) -> GrantBaseline | None:
    """*pin*'s frozen copy once the wheel copy is gone; None while the copy ships.

    While the copy ships, the record ``register_builtin_apps`` rewrites from it is
    the baseline and a ``deletedCopy`` on the row is never read. A row with neither
    vouches for nothing: its baseline is empty, so any grant the pinned tree
    declares is held back (``permission_widening``). Blocking: it scans the wheel.
    """
    if wheel_copy_ships(pin.name):
        return None
    return deleted_copy_baseline(json.loads(pin.deleted_copy) if pin.deleted_copy else {})

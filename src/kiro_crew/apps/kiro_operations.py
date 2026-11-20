"""Kiro operations: the Kiro BFF operations an app may call as the signed-in person.

An app names them in ``permissions.kiroOperations``, a list of exact operation
names (``["GetWidget", "ListWidgets"]``). Each one is an entry of the
:data:`GRANT_FAMILY` family in the app's approved-grants set, staged like
``permissions.api``: the install consent approves the list it shows, an update that
adds a name holds that name until the owner approves it on the app's page, and an
update that drops one needs nothing. The family is explicit-only, so a set stored
before this host knew the key approves none.

:func:`app_kiro_operations` is the one reader the call path uses
(``apps/kiro_calls.py``), read fresh on every call so a disable, an uninstall or
an update that drops a name ends it at the next call. The decision is exact set
membership: a name is checked character by character when parsed, and never
matched against a pattern.

Nothing here names an app.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The manifest key under ``permissions``, and the grant family that stores it.
GRANT_FAMILY = "kiroOperations"
KIRO_OPERATION_GRANT_FAMILIES = (GRANT_FAMILY,)
#: Most names one manifest may declare.
MAX_KIRO_OPERATIONS = 64
#: Longest operation name.
MAX_OPERATION_NAME = 64

_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_NAME_CHARS = _UPPER | frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def is_operation_name(value: Any) -> bool:
    """Whether *value* can name an operation: an upper-case ASCII letter, then letters or digits."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_OPERATION_NAME
        and value[0] in _UPPER
        and all(ch in _NAME_CHARS for ch in value)
    )


def declared_operations(value: Any) -> list[str]:
    """The valid names of a raw ``permissions.kiroOperations`` value, each once, in order."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(e for e in value if is_operation_name(e)))[:MAX_KIRO_OPERATIONS]


def kiro_operation_errors(value: Any) -> list[str]:
    """Validation errors of ``permissions.kiroOperations``; empty when valid or absent."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(e, str) for e in value):
        return ["permissions.kiroOperations must be an array of strings"]
    errors: list[str] = []
    if len(value) > MAX_KIRO_OPERATIONS:
        errors.append(
            f"permissions.kiroOperations: {len(value)} entries exceeds the limit of "
            f"{MAX_KIRO_OPERATIONS}"
        )
    for entry in value:
        if not is_operation_name(entry):
            errors.append(
                "permissions.kiroOperations entry is not an operation name (an upper-case "
                f"letter, then up to {MAX_OPERATION_NAME - 1} letters or digits): {entry!r}"
            )
    if len(set(value)) != len(value):
        errors.append("permissions.kiroOperations repeats an entry")
    return errors


def _declared(permissions: Any) -> list[str]:
    """The names a manifest's ``permissions`` (parsed or a raw dict) declare."""
    if isinstance(permissions, dict):
        return declared_operations(permissions.get(GRANT_FAMILY))
    return declared_operations(getattr(permissions, GRANT_FAMILY, None))


def kiro_operation_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: _declared(permissions)}


def _approved(app: str, declared: list[str]) -> list[str]:
    """The *declared* names in *app*'s approved-grants set, read fresh."""
    if not declared:
        return []
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, GRANT_FAMILY, lambda: declared))
    return [name for name in declared if name in approved]


def declared_kiro_operations(app: str) -> frozenset[str]:
    """The names *app*'s live manifest declares. Empty on any failure."""
    if not isinstance(app, str) or not app:
        return frozenset()
    try:
        from kiro_crew.apps.manager import get_app_manifest  # noqa: PLC0415

        manifest = get_app_manifest(app)
        return frozenset(_declared(manifest.permissions)) if manifest is not None else frozenset()
    except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
        logger.warning("could not read the Kiro operations an app declares", exc_info=True)
        return frozenset()


def kiro_app_enabled(app: str) -> bool:
    """Whether *app* is installed and enabled right now. False on any failure."""
    if not isinstance(app, str) or not app:
        return False
    try:
        from kiro_crew.apps.manager import is_app_enabled  # noqa: PLC0415

        return bool(is_app_enabled(app))
    except Exception:  # noqa: BLE001 - an unreadable record is not enabled
        logger.warning("could not read whether an app is enabled", exc_info=True)
        return False


def app_kiro_operations(app: str) -> frozenset[str]:
    """The operations *app* may call right now. Blocking; fails closed.

    The names the live manifest declares that the owner's approved set holds,
    while the app is enabled and trusted to execute. Empty when the app is not
    installed, not enabled, not trusted, or anything is unreadable.
    """
    if not isinstance(app, str) or not app:
        return frozenset()
    try:
        from kiro_crew.apps.execution import third_party_ceiling_closed  # noqa: PLC0415
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled  # noqa: PLC0415

        if not is_app_enabled(app):
            return frozenset()
        manifest = get_app_manifest(app)
        if manifest is None:
            return frozenset()
        approved = frozenset(_approved(app, _declared(manifest.permissions)))
        if approved and third_party_ceiling_closed(app) is not None:
            return frozenset()
        return approved
    except Exception:  # noqa: BLE001 - an unreadable grant grants nothing
        logger.warning("could not resolve the Kiro operations of an app", exc_info=True)
        return frozenset()


def kiro_operations_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``kiroOperationsGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared names the owner's approved set holds; ``staged``
    the declared ones it does not, which the detail page offers for approval.
    """
    permissions = (
        manifest.get("permissions")
        if isinstance(manifest, dict)
        else getattr(manifest, "permissions", None)
    )
    declared = _declared(permissions)
    if not declared:
        return {}
    approved = _approved(name, declared)
    staged = [e for e in declared if e not in approved]
    return {"kiroOperationsGrant": {"approved": approved, "staged": staged}}


__all__ = [
    "GRANT_FAMILY",
    "KIRO_OPERATION_GRANT_FAMILIES",
    "MAX_KIRO_OPERATIONS",
    "MAX_OPERATION_NAME",
    "app_kiro_operations",
    "declared_kiro_operations",
    "declared_operations",
    "is_operation_name",
    "kiro_app_enabled",
    "kiro_operation_errors",
    "kiro_operation_grant_entries",
    "kiro_operations_grant_row",
]

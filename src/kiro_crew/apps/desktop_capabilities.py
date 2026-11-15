"""Desktop capabilities: what an app's hosted desktop windows may ask the shell to do.

The desktop shell's window host (``website/electron/app-window-host.js``) hosts the
windows an app declares in its manifest ``desktop`` block. Beyond shaping its own
windows, such a page may need three things only the shell can do. An app names the
ones it uses in ``permissions.desktopCapabilities``:

* ``"screenCapture"``: the page may call ``getDisplayMedia`` and be granted a screen,
  the way the shell's own snip surfaces are (``capture-trust.js``).
* ``"localFiles"``: the page may ask the shell to reveal a local file in the system
  file manager, and to open a local image in the system viewer.
* ``"gatewayChoice"``: the page may list the operator's configured gateways
  (``/api/instances``, SSH ones included) and point the app's windows at one of them.

Each capability is an entry of the :data:`GRANT_FAMILY` family in the app's
approved-grants set. An install approves what it declares; an update that adds one
holds it back until the owner approves it on the app's page, like a new API path. The
family is registered explicit-only (``point_grants``): a stored set written before
this host knew the key approves no capability, because the owner was never shown it.
A built-in stores no approved-grants set, so its packaged declaration is approved.

The gateway decides only what is declared and approved; it publishes that on the app
row as ``desktopCapabilitiesGrant``. The shell is the enforcement point: it serves a
capability only to a window of an enabled app whose row lists it as approved, and it
refuses every other call. Nothing here names an app.
"""

from __future__ import annotations

from typing import Any

#: The manifest key under ``permissions``, and the grant family that stores it.
GRANT_FAMILY = "desktopCapabilities"
DESKTOP_CAPABILITY_GRANT_FAMILIES = (GRANT_FAMILY,)
#: The capabilities an app may declare, in the order every list of them is kept.
CAPABILITIES = ("screenCapture", "localFiles", "gatewayChoice")


def declared_capabilities(value: Any) -> list[str]:
    """The known capabilities in a raw ``permissions.desktopCapabilities`` value.

    Anything but a list declares nothing, and an unknown entry is dropped, so a
    malformed value grants less, never more.
    """
    if not isinstance(value, list):
        return []
    return [cap for cap in CAPABILITIES if cap in value]


def _declared(permissions: Any) -> list[str]:
    """The capabilities a manifest's ``permissions`` (parsed or a raw dict) declare."""
    if isinstance(permissions, dict):
        return declared_capabilities(permissions.get(GRANT_FAMILY))
    return declared_capabilities(getattr(permissions, GRANT_FAMILY, None))


def desktop_capability_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: _declared(permissions)}


def _approved(app: str, declared: list[str]) -> list[str]:
    """The *declared* capabilities in *app*'s approved-grants set, read fresh."""
    if not declared:
        return []
    # Deferred: the facade's family imports ``point_grants``, which imports this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, GRANT_FAMILY, lambda: declared))
    return [cap for cap in declared if cap in approved]


def desktop_capabilities_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``desktopCapabilitiesGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared capabilities the owner's approved set holds, which
    is all the shell will serve; ``staged`` the declared ones it does not, which the
    detail page offers for approval.
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
    staged = [cap for cap in declared if cap not in approved]
    return {"desktopCapabilitiesGrant": {"approved": approved, "staged": staged}}


__all__ = [
    "CAPABILITIES",
    "DESKTOP_CAPABILITY_GRANT_FAMILIES",
    "GRANT_FAMILY",
    "declared_capabilities",
    "desktop_capabilities_grant_row",
    "desktop_capability_grant_entries",
]

"""``permissions.browser``: the verbs an app may use on the browser tabs of its own slots.

An app names what it uses in ``permissions.browser`` (step 1a of
``docs/request-for-change/rfc-app-sdk-browser.md``):

* ``"view"``: embed the live view of its own slot and draw over it.
* ``"read"``: read the page of its own slot.
* ``"act"``: operate the page of its own slot.

Each verb is an entry of the :data:`GRANT_FAMILY` family in the app's approved-grants
set (``member-records/app-grants/<app>.json``), so an app cannot approve itself. An
install approves no verb: the family is one of ``manager_records._OWNER_APPROVED_FAMILIES``
and the owner approves verbs on the app's page. An update that adds a verb holds that
verb back and keeps the ones already approved (``_approved_grants_after_manifest_change``).
The family is registered explicit-only (``point_grants``): a stored set written before
this host knew the key approves no verb.

All of it sits behind the ``app_browser_sdk`` feature preview (:data:`PREVIEW_NAME`),
default off. With the preview off ``ctx.browser`` is absent and an approved verb has
no effect; the manifest still installs and the trust prompt says the feature is off.

This module holds the grant family, the preview check and ``ctx.browser``
(:class:`BrowserSDK`), whose ``page(slot)`` hands out a
:class:`~kiro_crew.apps.browser_page_port.PagePort`: every page operation goes through
the one gateway function the Bot's ``browser`` tool uses too.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.apps.browser_page_port import PagePort, owns_slot

logger = logging.getLogger(__name__)

#: The manifest key under ``permissions``, and the grant family that stores its verbs.
GRANT_FAMILY = "browser"
BROWSER_GRANT_FAMILIES = (GRANT_FAMILY,)
#: The verbs an app may declare, in the order every list of them is kept.
VERBS = ("view", "read", "act")
#: The ``dashboard.feature_previews`` name, and ``mc-preview-app-browser-sdk`` in
#: ``website/src/utils/previewFlags.ts`` (the two lists are pinned equal).
PREVIEW_NAME = "app_browser_sdk"


def declared_verbs(value: Any) -> list[str]:
    """The known verbs in a raw ``permissions.browser`` value.

    Anything but a list declares nothing, and an unknown entry is dropped, so a
    malformed value grants less, never more.
    """
    if not isinstance(value, list):
        return []
    return [verb for verb in VERBS if verb in value]


def _declared(permissions: Any) -> list[str]:
    """The verbs a manifest's ``permissions`` (parsed or a raw dict) declare."""
    if isinstance(permissions, dict):
        return declared_verbs(permissions.get(GRANT_FAMILY))
    return declared_verbs(getattr(permissions, GRANT_FAMILY, None))


def browser_grant_entries(permissions: Any) -> dict[str, list[str]]:
    """The :data:`GRANT_FAMILY` entries a manifest's ``permissions`` declare."""
    return {GRANT_FAMILY: _declared(permissions)}


def _approved(app: str, declared: list[str]) -> list[str]:
    """The *declared* verbs in *app*'s approved-grants set, read fresh."""
    if not declared:
        return []
    # Deferred: the facade's family imports ``point_grants``, which registers this module.
    from kiro_crew.apps.manager import staged_app_grants  # noqa: PLC0415

    approved = set(staged_app_grants(app, GRANT_FAMILY, lambda: declared))
    return [verb for verb in declared if verb in approved]


def preview_on(cfg: Any = None) -> bool:
    """Whether the edition permits the preview and its stored value is exactly ``True``.

    Reads *cfg*, else the live configuration, else the config file. Unreadable is off.
    """
    try:
        if cfg is None:
            from kiro_crew.config import live  # noqa: PLC0415

            cfg = live.snapshot()
            if cfg is None:
                from kiro_crew.config.loader import KiroCrewConfig  # noqa: PLC0415

                cfg = KiroCrewConfig.load()
        from kiro_crew.edition_locks import feature_previews  # noqa: PLC0415

        previews = feature_previews(
            getattr(getattr(cfg, "dashboard", None), "feature_previews", None)
        )
    except Exception:  # noqa: BLE001 - unread means off
        logger.debug("app browser sdk: config unreadable; treating as off", exc_info=True)
        return False
    return isinstance(previews, Mapping) and previews.get(PREVIEW_NAME) is True


def app_browser_verbs(app: str) -> tuple[str, ...]:
    """The verbs *app* holds right now. Blocking; fails closed.

    Empty unless the preview is on, the app is enabled, the live manifest declares
    the verb and the owner approved it. Turning the app off keeps the approvals
    stored but holds every verb back until it is on again.
    """
    if not isinstance(app, str) or not app or not preview_on():
        return ()
    try:
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled  # noqa: PLC0415

        manifest = get_app_manifest(app)
        if manifest is None or not is_app_enabled(app):
            return ()
        return tuple(_approved(app, _declared(manifest.permissions)))
    except Exception:  # noqa: BLE001 - an unreadable grant grants nothing
        logger.warning("could not resolve the browser verbs of an app", exc_info=True)
        return ()


def browser_grant_row(name: str, manifest: Any) -> dict[str, Any]:
    """The ``browserGrant`` key of an app row, absent when nothing is declared.

    ``approved`` lists the declared verbs the owner's approved set holds; ``staged``
    the declared ones it does not, which the detail page offers for approval.
    ``preview`` says whether the ``app_browser_sdk`` preview is on, so the page can
    say an approved verb has no effect while it is off.
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
    staged = [verb for verb in declared if verb not in approved]
    return {"browserGrant": {"approved": approved, "staged": staged, "preview": preview_on()}}


class BrowserRefused(PermissionError):
    """``ctx.browser.page(slot)`` was refused; ``code`` says why (``not_your_slot``, ``no_state``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verb_refusal(app: str, verb: str) -> tuple[str, str] | None:
    """``(code, error)`` when *app* may not use *verb* now, else ``None``. Blocking.

    Read fresh at each call: the preview first (``preview_off``), then
    :func:`app_browser_verbs`, which also holds every verb back while the app is off.
    """
    if not preview_on():
        return (
            "preview_off",
            "the owner has not turned on App browser access (Settings > Developer > "
            "Feature previews)",
        )
    if verb not in app_browser_verbs(app):
        return (
            "permission_denied",
            f"browser {verb!r} is not approved for {app!r}, or the app is turned off",
        )
    return None


class BrowserSDK:
    """``ctx.browser``: an app's access to the browser tabs of its own slots.

    Built when the manifest declares ``permissions.browser``
    (``context.build_app_context``), and handed out as ``ctx.browser`` only while the
    ``app_browser_sdk`` preview is on (``AppContext.browser``). The verbs are
    re-read at each call, so an approval, a revoke, the preview turned off or the app
    turned off takes effect without a restart, on a port already handed out too.
    """

    def __init__(self, app_name: str, *, http_app: Any = None) -> None:
        """*http_app* is the gateway's Application, read for the dashboard state when a
        :meth:`page` call is not handed one (as ``ChatSlotSDK`` does)."""
        self._app = app_name
        self._http_app = http_app

    def verbs(self) -> tuple[str, ...]:
        """The verbs this app holds now: declared, approved, and the preview on."""
        return app_browser_verbs(self._app)

    def _audit_denied(self, code: str) -> None:
        """The audit row a refused ``page`` writes, as the app, like every ``PagePort`` refusal."""
        from kiro_crew.dashboard.handlers import browser_ops  # noqa: PLC0415

        caller: Any = browser_ops.GatewayCaller(f"app:{self._app}")
        browser_ops._audit(caller, "denied", code)

    def page(self, slot: str, *, state: Any = None) -> PagePort:
        """The page of *slot*, one of this app's own live chat slots.

        *state* is the dashboard state a route gets as ``request.app["state"]``;
        without it, the one on the gateway's Application. :class:`BrowserRefused`
        when *slot* is not this app's (key prefix AND stamp) or no state is at hand.
        The port checks the slot and the verb again at every call.
        """
        if state is None:
            getter = getattr(self._http_app, "get", None)
            state = getter("state") if callable(getter) else None
        if state is None:
            self._audit_denied("no_state")
            raise BrowserRefused("no_state", "no dashboard state to find the slot in")
        if not owns_slot(state, self._app, slot):
            self._audit_denied("not_your_slot")
            raise BrowserRefused("not_your_slot", f"{slot!r} is not one of {self._app!r}'s slots")
        app = self._app
        return PagePort(app, slot, state=state, verb_gate=lambda verb: verb_refusal(app, verb))


def browser_sdk_for(
    app_name: str, permissions: Mapping[str, Any], *, http_app: Any = None
) -> BrowserSDK | None:
    """The SDK to keep on *app_name*'s context, or ``None`` when *permissions* has no ``browser``.

    It does not read the preview: ``AppContext.browser`` does, at each access, so a
    preview turned on after the app started needs no restart.
    """
    if GRANT_FAMILY not in permissions:
        return None
    return BrowserSDK(app_name, http_app=http_app)


__all__ = [
    "BROWSER_GRANT_FAMILIES",
    "BrowserRefused",
    "BrowserSDK",
    "GRANT_FAMILY",
    "PREVIEW_NAME",
    "VERBS",
    "app_browser_verbs",
    "browser_grant_entries",
    "browser_grant_row",
    "browser_sdk_for",
    "declared_verbs",
    "preview_on",
]

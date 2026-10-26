"""Governance keys for extensions, plugins, app versions and app developer mode.

RFC ``rfc-crew-enterprise-edition``, appendix rows "Extension install", "Extension
uninstall", "Plugin sync", "App versions" and "App developer mode". Each check asks
the governance ceiling and the host profile, so a remote policy can turn the control
on and a profile can only narrow it. In an enterprise build a scope the policy does
not name answers the built-in baseline, where all three are off. In the ``oss``
edition an unnamed scope permits, so nothing changes until a policy names one.

* :data:`EXTENSIONS_SCOPE` turns installing on or off. Its ``extensions`` list
  names what may be installed, as ``<kind>:<id>`` (``mcp:github``,
  ``skill:pdf-tools``, ``agent:reviewer``, ``plugin:reviewer``). Uninstall needs
  the scope on; the list does not bind it, because removing only narrows.
* :data:`APP_VERSIONS_SCOPE` lists the app versions that may be installed and
  loaded, as ``<app>@<version>`` (``file-explorer@1.*``).
* :data:`DEV_MODE_SCOPE` turns app developer mode on or off.
  :func:`developer_mode_permitted` is the one reader for any other developer-mode
  switch.

Every check is fail-closed: an evaluation error denies. A denial and a governed
permit are written to the SEL; a permit nothing governs is not, so an ungoverned
``oss`` host records nothing new. Every check reads the profile store, which stats
the profiles directory, so an async caller runs it off the event loop.
"""

from __future__ import annotations

import logging
from typing import Optional

from kiro_crew import edition

logger = logging.getLogger(__name__)

EXTENSIONS_SCOPE = "capabilities.extension_install"
EXTENSIONS_LIST = "extensions"
APP_VERSIONS_SCOPE = "app_versions"
DEV_MODE_SCOPE = "capabilities.app_dev_mode"

#: Extension kinds an ``extensions`` list entry may name.
EXTENSION_KINDS = frozenset({"mcp", "skill", "agent", "plugin"})


def _denied(
    scope: str, item: str, *, action: str, audit: bool = True, unknown: str = ""
) -> Optional[str]:
    """The denial reason for *item* in *scope* on the host surface, or ``None``.

    *unknown* names a missing input: then any governed answer refuses with it, and
    only an ungoverned scope permits, because a list cannot judge what it is not
    shown.
    """
    try:
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY, governance_permits

        decision = governance_permits(
            scope, item, session_key=HOST_SESSION_KEY, log_warning=False, fail_closed=True
        )
        if getattr(decision, "layer", None) == "" and edition.is_enterprise():
            # ``governance_permits`` answers "ungoverned" when there is no ceiling
            # and no profile (left to the enterprise gate, RFC phase 2). These keys
            # answer the enterprise baseline there instead, so they stay off.
            from kiro_crew.platform.governance import resolve

            decision = resolve(None, None, scope, item)
    except Exception as exc:  # noqa: BLE001 - an unevaluable ceiling denies
        logger.warning("%s: governance unavailable (%s); denying", action, type(exc).__name__)
        decision = None
    permitted = decision is not None and getattr(decision, "permitted", False)
    reason = (
        None
        if permitted
        else (getattr(decision, "reason", "") or "governance unavailable (fail-closed)")
    )
    # A permit nothing governs (layer ``default``, or none for the no-ceiling fast
    # path) is not recorded, so an ungoverned host writes nothing new; a governed
    # permit and every denial are.
    ungoverned = permitted and getattr(decision, "layer", "") in ("", "default")
    if permitted and unknown and not ungoverned:
        permitted, reason = False, unknown
    if audit and not ungoverned:
        try:
            from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
            from kiro_crew.sel import sel

            sel().log_governance_decision(
                session_key=HOST_SESSION_KEY,
                tool_name=action,
                scope=scope,
                item=item,
                outcome="allowed" if permitted else "denied",
                rule=getattr(decision, "rule", ""),
                layer=getattr(decision, "layer", ""),
                reason=str(reason or getattr(decision, "reason", "")),
            )
        except Exception:
            logger.debug("%s governance audit failed", action, exc_info=True)
    return reason


def extension_install_denied(kind: str, ident: str) -> Optional[str]:
    """Why installing extension *ident* of *kind* is refused, or ``None``."""
    if kind not in EXTENSION_KINDS:
        raise ValueError(f"unknown extension kind {kind!r}")
    return _denied(EXTENSIONS_SCOPE, f"{EXTENSIONS_LIST}:{kind}:{ident}", action=f"install_{kind}")


def extensions_off(action: str) -> Optional[str]:
    """Why *action* is refused because extension installing is off, or ``None``.

    The ``extensions`` list is not asked: uninstall only narrows what is installed.
    """
    return _denied(EXTENSIONS_SCOPE, "", action=action)


def app_version_denied(name: str, version: str) -> Optional[str]:
    """Why app *name* at *version* may not be installed or loaded, or ``None``.

    A *version* that is not a semver string (empty, or a JSON ``null`` that a
    manifest parser turned into ``"None"``) is refused under any policy that
    governs ``app_versions``: a list cannot judge it.
    """
    from kiro_crew.apps.manifest_names import SEMVER_RE

    readable = bool(version) and SEMVER_RE.match(version) is not None
    return _denied(
        APP_VERSIONS_SCOPE,
        f"{name}@{version}",
        action=f"app_version:{name}",
        unknown="" if readable else "the app declares no readable version",
    )


def developer_mode_permitted(*, audit: bool = False) -> bool:
    """Whether governance lets developer mode be on. Fail-closed.

    Reads the profile store, which stats the profiles directory: call it off the
    event loop. Pass ``audit=True`` at a switch, so the decision is recorded; reads
    leave ``audit`` off.
    """
    return _denied(DEV_MODE_SCOPE, "", action="developer_mode", audit=audit) is None

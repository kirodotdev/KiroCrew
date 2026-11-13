"""Governance keys for extensions, plugins, app versions, app developer mode and
extension sources.

RFC ``rfc-crew-lts-edition``, appendix rows "Extension install", "Extension
uninstall", "Plugin sync", "App versions", "App developer mode" and "Extension
sources". Each check asks the governance ceiling and the host profile, so a remote
policy can turn the control on and a profile can only narrow it. In an LTS build a
scope the policy does not name answers the built-in baseline, where all of them
but extension sources are off. In the ``oss`` edition an unnamed scope permits, so
nothing changes until a policy names one.

* :data:`EXTENSIONS_SCOPE` turns installing on or off. Its ``extensions`` list
  names what may be installed, as ``<kind>:<id>`` (``mcp:github``,
  ``skill:pdf-tools``, ``agent:reviewer``, ``plugin:reviewer``). Uninstall needs
  the scope on; the list does not bind it, because removing only narrows.
* :data:`APP_VERSIONS_SCOPE` lists the app versions that may be installed and
  loaded, as ``<app>@<version>`` (``file-explorer@1.*``).
* :data:`DEV_MODE_SCOPE` turns app developer mode on or off.
  :func:`developer_mode_permitted` is the one reader for any other developer-mode
  switch.
* :data:`REGISTRY_HOSTS_SCOPE` lists the hosts of the skill and MCP discovery
  registries the Discover surfaces may query (``skills.sh``,
  ``registry.modelcontextprotocol.io``). :func:`registry_host_denied` is asked by
  ``dashboard.handlers._shared_access.admits_registry``. It is open when unnamed
  in every edition (``BASELINE_UNGATED``), so only a policy that names it narrows.

Every check but :func:`registry_host_denied` is fail-closed: an evaluation error
denies. That one admits on an evaluation error, logged and audited as
``governance_degraded``, as Discover did before the key existed. A denial and a
governed permit are written to the SEL; a permit nothing governs is not, so an
ungoverned ``oss`` host records nothing new. Every check reads the profile store,
which stats the profiles directory, so an async caller runs it off the event loop.
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
REGISTRY_HOSTS_SCOPE = "external_access.registry_hosts"

#: Extension kinds an ``extensions`` list entry may name.
EXTENSION_KINDS = frozenset({"mcp", "skill", "agent", "plugin"})


def _denied(
    scope: str,
    item: str,
    *,
    action: str,
    audit: bool = True,
    unknown: str = "",
    fail_closed: bool = True,
) -> Optional[str]:
    """The denial reason for *item* in *scope* on the host surface, or ``None``.

    *unknown* names a missing input: then any governed answer refuses with it, and
    only an ungoverned scope permits, because a list cannot judge what it is not
    shown.

    ``fail_closed=False`` admits when governance cannot be evaluated: the error is
    logged and written to the SEL as ``governance_degraded``, the same record
    ``governance_permits`` writes for its own degrade. A ``PlatformCompositionError``
    still propagates.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    try:
        from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY, governance_permits

        decision = governance_permits(
            scope, item, session_key=HOST_SESSION_KEY, log_warning=False, fail_closed=fail_closed
        )
        if getattr(decision, "layer", None) == "" and edition.is_lts():
            # ``governance_permits`` answers "ungoverned" when there is no ceiling
            # and no profile (left to the LTS gate, RFC phase 2). These keys
            # answer the LTS baseline there instead.
            from kiro_crew.platform.governance import resolve

            decision = resolve(None, None, scope, item)
    except PlatformCompositionError:
        if fail_closed:
            logger.warning("%s: governance unavailable (PlatformCompositionError); denying", action)
            decision = None
        else:
            raise
    except Exception as exc:  # noqa: BLE001 - an unevaluable ceiling denies
        if not fail_closed:
            _audit_degraded(action, scope)
            return None
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


def _audit_degraded(action: str, scope: str) -> None:
    """Log and SEL-record that governance could not answer *action*; admitting."""
    try:
        from kiro_crew.platform.governance_profiles import (
            HOST_SESSION_KEY,
            audit_governance_degraded,
        )

        audit_governance_degraded(action, session_key=HOST_SESSION_KEY, scope=scope)
    except Exception:  # noqa: BLE001 - the admit stands; the log still says why
        logger.warning("%s: governance unavailable; admitting", action, exc_info=True)


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


def registry_host_denied(kind: str, api_base: str) -> Optional[str]:
    """Why governance refuses the discovery registry at *api_base*, or ``None``.

    The item is the host of *api_base* (``https://skills.sh/api`` is
    ``skills.sh``), never the provider's own name, so an allowlist stops admitting
    a provider that repoints at another host. A base with no network host is
    judged as ``""``, which an allow list admits only through a pattern that
    matches it, such as ``*``. One list covers both catalogs: *kind* (``skill`` or
    ``mcp``) only labels the audit record.

    Unlike the keys above it is open when unnamed, in every edition, and a
    governance error admits (logged and audited as ``governance_degraded``), as
    Discover did before the key existed: only a policy that names it narrows.

    Reads the profile store: call it off the event loop.
    """
    from kiro_crew.platform.governance_parts.matching import _url_host

    return _denied(
        REGISTRY_HOSTS_SCOPE,
        _url_host(api_base or ""),
        action=f"registry:{kind}",
        fail_closed=False,
    )

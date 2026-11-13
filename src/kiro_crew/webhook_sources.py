"""Which inbound webhook sources the governance policy permits: the ``webhooks`` scope.

A policy names the sources that may start work from an inbound request (RFC
``rfc-crew-lts-edition``, appendix rows "Incoming webhooks" and Red
"Webhooks by source"), e.g. ``{"webhooks": {"mode": "allow", "allow": ["agent"]}}``.
Each source asks :func:`source_refusal` at its own entry, before its token or
signature is checked:

* :data:`SOURCE_AGENT` -- ``POST /api/hooks/agent`` (``kiro_crew.webhooks``);
* an app whose manifest declares ``permissions.webhookSource`` -- that app's own
  inbound route, governed under the app's name (:func:`app_source_refusal`). The
  app does not choose the id: it is always the name the app is installed under, so
  a policy that names an app keeps naming it whether the app ships in the wheel or
  from its own repository.

With no policy the scope is ungoverned and every source keeps its own switch, so an
``oss`` build behaves as before. In an LTS build an unnamed scope is answered
by the built-in baseline (off), and only a permit the policy layer gave counts
(:func:`kiro_crew.edition_locks.follows_policy`); an evaluation error refuses there.
Policy only: an inbound request carries no session, so no per-surface profile is asked.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The governed scope; members are source ids.
WEBHOOKS_SCOPE = "webhooks"

#: ``POST /api/hooks/agent``, the core agent-turn webhook.
SOURCE_AGENT = "agent"

#: The core's own source ids. An app installed under one of these names is never
#: governed as that source: a policy permitting the core webhook must not permit an
#: app that happens to share its id.
CORE_SOURCES = frozenset({SOURCE_AGENT})


def source_refusal(source: str) -> str:
    """Why the policy refuses inbound webhooks from *source*; ``""`` when it permits.

    Blocking (the governance answer can read the profile store): an async caller
    runs this off the event loop. In an ``oss`` build a ``PlatformCompositionError``
    propagates, as at every other governance chokepoint; an LTS build
    refuses instead, since every source is off there until a policy names it.
    """
    from kiro_crew.edition_locks import UNNAMED_REASON, follows_policy, locked_off
    from kiro_crew.platform.context import PlatformCompositionError

    lts = locked_off()
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(WEBHOOKS_SCOPE, source, log_warning=False, fail_closed=lts)
    except PlatformCompositionError:
        # LTS: no composed context is no policy, so the source stays off.
        if lts:
            return f"webhooks {UNNAMED_REASON}"
        raise
    except Exception:
        logger.debug("webhook source governance unavailable", exc_info=True)
        return "webhooks governance unavailable (fail-closed)" if lts else ""
    if not getattr(decision, "permitted", False):
        return f"webhook source {source!r} refused by the security policy"
    if not follows_policy(decision):
        return f"webhooks {UNNAMED_REASON}"
    return ""


def app_declares_webhook_source(app_name: str) -> bool:
    """Whether *app_name*'s manifest declares ``permissions.webhookSource``.

    Read from the installed record's ``app.json`` (a built-in's is rewritten from
    the wheel at every start); before that copy exists, from the shipped package
    while the wheel copy is live. A manifest that cannot be read declares nothing.
    """
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manager import get_app_manifest
    from kiro_crew.apps.manifest import AppManifest

    manifest = get_app_manifest(app_name)
    if manifest is None:
        root = shipped_builtin_app_root(app_name)
        if root is None:
            return False
        try:
            manifest = AppManifest.from_json_file(root / "app.json")
        except Exception:  # noqa: BLE001 - an unreadable manifest declares nothing
            logger.debug("shipped manifest of %s unreadable", app_name, exc_info=True)
            return False
    return manifest.permissions.webhookSource is True


def app_source_refusal(app_name: str) -> str:
    """Why inbound webhooks to app *app_name* are refused; ``""`` when permitted.

    The app's manifest must declare ``permissions.webhookSource``, its name must not
    be a core source id, and then :func:`source_refusal` answers for the source
    named *app_name*. Blocking, like :func:`source_refusal`.
    """
    if app_name in CORE_SOURCES:
        return f"app {app_name!r} shares a core webhook source id"
    if not app_declares_webhook_source(app_name):
        return f"app {app_name!r} declares no webhook source"
    return source_refusal(app_name)

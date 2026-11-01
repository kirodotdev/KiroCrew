"""Which inbound webhook sources the governance policy permits: the ``webhooks`` scope.

A policy names the sources that may start work from an inbound request (RFC
``rfc-crew-enterprise-edition``, appendix rows "Incoming webhooks" and Red
"Webhooks by source"), e.g. ``{"webhooks": {"mode": "allow", "allow": ["agent"]}}``.
Each source asks :func:`source_refusal` at its own entry, before its token or
signature is checked:

* :data:`SOURCE_AGENT` -- ``POST /api/hooks/agent`` (``kiro_crew.webhooks``);
* :data:`SOURCE_OPS_MISSION_CONTROL` -- the Ops Mission Control app's signal webhook.

With no policy the scope is ungoverned and every source keeps its own switch, so an
``oss`` build behaves as before. In an enterprise build an unnamed scope is answered
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

#: The Ops Mission Control app's signal webhook (its app id).
SOURCE_OPS_MISSION_CONTROL = "ops-mission-control"


def source_refusal(source: str) -> str:
    """Why the policy refuses inbound webhooks from *source*; ``""`` when it permits.

    Blocking (the governance answer can read the profile store): an async caller
    runs this off the event loop. In an ``oss`` build a ``PlatformCompositionError``
    propagates, as at every other governance chokepoint; an enterprise build
    refuses instead, since every source is off there until a policy names it.
    """
    from kiro_crew.edition_locks import UNNAMED_REASON, follows_policy, locked_off
    from kiro_crew.platform.context import PlatformCompositionError

    enterprise = locked_off()
    try:
        from kiro_crew.platform.governance_profiles import governance_permits

        decision = governance_permits(
            WEBHOOKS_SCOPE, source, log_warning=False, fail_closed=enterprise
        )
    except PlatformCompositionError:
        # Enterprise: no composed context is no policy, so the source stays off.
        if enterprise:
            return f"webhooks {UNNAMED_REASON}"
        raise
    except Exception:
        logger.debug("webhook source governance unavailable", exc_info=True)
        return "webhooks governance unavailable (fail-closed)" if enterprise else ""
    if not getattr(decision, "permitted", False):
        return f"webhook source {source!r} refused by the security policy"
    if not follows_policy(decision):
        return f"webhooks {UNNAMED_REASON}"
    return ""

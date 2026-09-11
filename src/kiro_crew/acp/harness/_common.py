"""Seams every harness answers the same way, and the kiro-family vocabulary.

Two kinds of thing live here.

:class:`MembershipHarness` implements the seams whose answer is a LOOKUP rather
than a judgement -- sandbox delegation, pod HOME remapping, recycle thresholds.
Each is one read of a membership set keyed by the harness's own backend id, so a
subclass gets the right answer by declaring its id and nothing else. This is what
keeps the promise that the harness layer re-declares no membership: there is
exactly one implementation of each of these questions in the tree, and it reads
the set that already owns the answer.

A lookup only earns a member here when a CALLER asks the harness for it. Routing
and the model verb are looked up by both drivers straight from their tables, so
mirroring them would add a second declaration rather than remove one.

:data:`KIRO_FAMILY_ALIASES` is the ``_kiro.dev/*`` notification vocabulary that
kiro-cli and the KAS relay both speak, because KAS is reached THROUGH kiro-cli's
own ACP relay. A host outside that family declares its own.
"""

from __future__ import annotations

from kiro_crew.acp.harness.base import (
    HarnessAdapter,
    NotificationAliases,
    ReclaimPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_POD_HOME_REMAP,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_SESSION_UPDATE,
    METHOD_SUBAGENT_LIST_UPDATE,
)

__all__ = ["KIRO_FAMILY_ALIASES", "MembershipHarness"]


#: The notification spellings kiro-cli and the KAS relay share.
#:
#: ``session_update`` carries BOTH names because kiro-cli sends the standard one
#: and its ``_kiro.dev`` alias for the same event; accepting only one silently
#: drops half the updates on whichever version disagrees.
KIRO_FAMILY_ALIASES = NotificationAliases(
    session_update=(METHOD_SESSION_UPDATE, METHOD_KIRO_SESSION_UPDATE),
    subagent_list_update=METHOD_SUBAGENT_LIST_UPDATE,
    mcp_init=(
        METHOD_MCP_OAUTH_REQUEST,
        METHOD_MCP_SERVER_INITIALIZED,
        METHOD_MCP_SERVER_INIT_FAILURE,
    ),
)


class MembershipHarness(HarnessAdapter):
    """The lookup-shaped seams, implemented once against the membership sets.

    A subclass sets :attr:`~kiro_crew.acp.harness.base.HarnessAdapter.backend`
    and inherits correct answers here. It must still answer every judgement-shaped
    seam itself -- argv, handshake, extras, callbacks, aliases, teardown -- which is
    why those stay abstract on the base class.
    """

    @property
    def internal_sandbox(self) -> bool:
        return self.backend in ACP_BACKENDS_INTERNAL_SANDBOX

    @property
    def pod_home_remap(self) -> bool:
        return self.backend in ACP_BACKENDS_POD_HOME_REMAP

    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """Pass the runtime's configured thresholds straight through.

        A host that leaks faster overrides this and narrows them; nothing in the
        kiro family does, so the operator's configuration is the whole answer.
        """
        return ReclaimPolicy(max_age_secs=max_age_secs, max_rss_mb=max_rss_mb)

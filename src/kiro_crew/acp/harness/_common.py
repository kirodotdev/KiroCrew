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

import os

from kiro_crew import agent as agent_mod
from kiro_crew import kiro_cli
from kiro_crew.acp.harness.base import (
    HarnessAdapter,
    NotificationAliases,
    ReclaimPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKENDS_CLIENT_META_SETTINGS,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS,
    ACP_BACKENDS_POD_HOME_REMAP,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_SESSION_UPDATE,
    METHOD_SUBAGENT_LIST_UPDATE,
)

__all__ = [
    "KIRO_FAMILY_ALIASES",
    "MANDATORY_MCPS_ENV",
    "MembershipHarness",
    "pin_mandatory_mcps_env",
]

#: kiro-cli keeps every server named here out of Tool Search deferral. It reads
#: the variable from the process environment once, at spawn.
MANDATORY_MCPS_ENV = "ASBX_KIRO_MANDATORY_MCPS"


def pin_mandatory_mcps_env(env: dict[str, str], *, spawned_binary: str | None = None) -> None:
    """Pin Tool Search exemptions to the operator's value or the engine's version.

    An ambient value wins verbatim, including an empty one; per-session overlays
    never decide the list. Crew's servers defer only when the spawn runs the pinned
    kiro-cli install (or its chat sibling) at >= 2.27.0. Any other executable, older or
    unknown versions keep every Crew-owned server resident to avoid the
    thinking-signature "tools list differs" rejection that bricks a session.
    """
    ambient = os.environ.get(MANDATORY_MCPS_ENV)
    if ambient is not None:
        env[MANDATORY_MCPS_ENV] = ambient
        return

    if kiro_cli.mandatory_mcps_drop_allowed(spawned_binary):
        env.pop(MANDATORY_MCPS_ENV, None)
        return

    # Not emission_eligible_mcp_servers(): granted opt-in servers serve tools too.
    servers = agent_mod.crew_owned_mcp_servers()
    if servers:
        env[MANDATORY_MCPS_ENV] = ",".join(sorted(servers))
    else:
        env.pop(MANDATORY_MCPS_ENV, None)


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

    @property
    def reads_markdown_agent_specs(self) -> bool:
        return self.backend in ACP_BACKENDS_MARKDOWN_AGENT_SPECS

    @property
    def client_meta_settings(self) -> bool:
        return self.backend in ACP_BACKENDS_CLIENT_META_SETTINGS

    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """Pass the runtime's configured thresholds straight through.

        A host that leaks faster overrides this and narrows them; nothing in the
        kiro family does, so the operator's configuration is the whole answer.
        """
        return ReclaimPolicy(max_age_secs=max_age_secs, max_rss_mb=max_rss_mb)

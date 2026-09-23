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
from kiro_crew.agent_sdk.tool_search import MANDATORY_MCPS_ENV, mandatory_mcps_env_value

__all__ = ["KIRO_FAMILY_ALIASES", "MembershipHarness", "apply_mandatory_mcps_env"]


def apply_mandatory_mcps_env(env: dict[str, str]) -> None:
    """Exempt Crew's own MCP servers from kiro-cli's Tool Search deferral.

    Lives here because BOTH kiro-family harnesses need it and the reason is the
    same one this module exists for: KAS is reached through kiro-cli's own ACP
    relay. Crew launches it as ``kiro-cli acp --agent-engine v3``, the same ``acp``
    subcommand the kiro path uses, and that subcommand reads
    :data:`~kiro_crew.agent_sdk.tool_search.MANDATORY_MCPS_ENV` unconditionally --
    the read is not gated on ``--agent-engine``. So the exemption applies to both,
    and stating it once is what keeps them from drifting.

    **Why the exemption exists.** Loading a deferred MCP spec REWRITES the
    request's ``tools`` array, and an extended-thinking model's thinking blocks
    carry a signature bound to the array they were minted under. Replay one across
    a load and the provider rejects the whole request -- "The ``tools`` list differs
    from the one this block was created with" -- and because it is rejecting the
    conversation's history, every later turn fails identically. The session is
    bricked, not slowed. Crew's own servers are what churn the array: they are the
    infrastructure an agent reaches for in nearly every session. Third-party
    servers keep deferring -- they carry most of the spec weight and are reached
    rarely.

    **The operator's AMBIENT value wins; a per-session overlay never does.** That
    distinction is why this reads :data:`os.environ` rather than the *env* mapping
    handed in. That mapping is already ``{**os.environ, **extra_env}``
    (``acp.runtime._spawn_admitted``), and ``extra_env`` carries per-session
    overlays -- among them a cron job's own ``env`` block, which
    ``cron_job_env_without_reserved`` passes through for every key outside
    ``_CRON_RESERVED_ENV_KEYS``, and which an app manifest's ``crons[].env`` can
    author. An overlay that set this key to ``""`` would suppress the exemption for
    that cron's sessions, and the variable is fixed at spawn, so the next run
    inherits the same manifest and re-bricks: no code-level recovery, and the
    rejected history is gone. So an overlay value is overwritten, and removed
    outright when Crew has no servers to name -- an overlay may neither disable
    this nor invent it.

    An ambient value is a different actor: the operator's own environment, honoured
    verbatim **including an explicit empty one**. Empty is the only spelling that
    says "exempt nothing", since the engine reads an empty variable as an absent
    one, so truthiness would leave that choice unexpressible.

    Applied regardless of the Tool Search toggle: the engine ignores the list while
    deferral is off, so a value that does not depend on the toggle cannot disagree
    with it, and a resume cannot arrive carrying a different exemption than the
    spawn it resumes.

    Mutates *env* in place, matching the ``apply_spawn_env`` contract it serves.
    """
    ambient = os.environ.get(MANDATORY_MCPS_ENV)
    if ambient is not None:
        env[MANDATORY_MCPS_ENV] = ambient
        return
    mandatory = mandatory_mcps_env_value(agent_mod.crew_owned_mcp_servers())
    if mandatory:
        env[MANDATORY_MCPS_ENV] = mandatory
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

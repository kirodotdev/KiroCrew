"""What Crew's own control plane needs on a ``session/new`` MCP element.

Shared by every mirror that projects the agent spec into an ``mcpServers`` array,
and shared rather than copied for a reason that is about SECURITY DRIFT rather
than about line count. Three rules live here, and each one is a rule a second copy
would eventually stop matching:

1. Which env a control-plane element carries (``KIROCREW_SESSION_KEY`` and the
   three values that make it usable). A key added for one backend and missed by
   another does not fail: the second backend's control plane simply comes up
   unable to do the thing the new key enabled, silently.
2. Which of Crew's OWN managed servers must not be mounted at all, because this
   channel cannot hand them a session identity and they would answer ``not_bound``
   to every call. DERIVED from the managed set, never enumerated -- a server added
   to that set later must land here by construction.
3. That the identity rides ONLY the control plane. An element the agent spec
   describes is one whose command, args and env the spec chose, so handing it this
   session's credential would let a hand-edited line drive the session it was
   mounted into. That line is the same on every transport, and a mirror author
   copying an ``_identity_env`` helper is exactly the person who might place the
   call one element wider.

The REASON a transport needs the carriage differs per backend, and that difference
belongs in each mirror's docstring: codex-rs ``env_clear()``s its stdio children
and re-adds an allowlist, so nothing reaches them by inheritance, while opencode's
children inherit the ambient environment and the element ``env`` is what makes the
value session-SCOPED rather than gateway-scoped. Same carriage, different argument
for it -- which is why the argument stays with the mirror and the carriage lives
here.

``label`` is the backend's name in the log lines, so a warning still says which
projection was resolving when a config-plane read failed.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

logger = logging.getLogger(__name__)


def control_plane_identity_env(session_key: str, channel_id: str, *, label: str) -> dict[str, str]:
    """The env Crew's own control plane needs, resolved for this session.

    Resolved live rather than read from the spec, exactly as ``managed_mcp_spec_entry``
    resolves the command -- the spec is hand-editable and this is an identity.

    ``KIROCREW_BOUND_PORT`` for the same reason ``members.member_dispatch_session_server``
    carries it: without the port the child falls through to the run marker, whose
    check needs ``find_listening_pids`` (``lsof``), which sees no listener from
    inside a sandbox's user namespace -- so the child dials the default port and
    every call is a connection refused on a gateway bound anywhere else.

    Fail-soft throughout: a config-plane failure must not fail a spawn, and no home
    override is exactly the state a default install is in.
    """
    # circular import: agent's module graph is heavy (it imports config), and
    # port_resolution reaches config.loader, whose provider-backend path imports
    # members. Both resolved at call time, as session_mcp.py resolves them.
    from kiro_crew.agent import _managed_mcp_env
    from kiro_crew.port_resolution import resolve_serving_port

    env: dict[str, str] = {}
    try:
        env.update(_managed_mcp_env())
    except Exception:  # pragma: no cover - defensive; the helper is fail-soft
        logger.warning("%s session MCP: could not resolve the managed home", label, exc_info=True)
    if session_key:
        env["KIROCREW_SESSION_KEY"] = session_key
    if channel_id:
        env["KIROCREW_CHANNEL_ID"] = channel_id
    try:
        env["KIROCREW_BOUND_PORT"] = str(resolve_serving_port())
    except Exception:  # pragma: no cover - defensive
        logger.warning("%s session MCP: could not resolve the serving port", label, exc_info=True)
    return env


def with_env(element: dict[str, Any], extra: Mapping[str, str]) -> dict[str, Any]:
    """*element* with *extra* merged into its ACP array-of-pairs ``env``.

    Later wins, so a value resolved here replaces a stale one the entry carried --
    the same precedence ``managed_mcp_spec_entry`` applies to the command.
    """
    pairs: list[dict[str, str]] = [
        p for p in element.get("env") or [] if isinstance(p, dict) and p.get("name") not in extra
    ]
    pairs.extend({"name": k, "value": v} for k, v in extra.items())
    out = dict(element)
    out["env"] = pairs
    return out


def identity_bound_crew_servers() -> frozenset[str]:
    """Crew's own managed servers that mounting would leave UNUSABLE on an array.

    Every managed server minus the control plane. The control plane is the part a
    projection rebuilds with this session's identity; everything else reaches the
    backend from the agent spec unreplaced, so it would come up bound to no session
    and answer ``not_bound`` to every call. That present-but-unusable shape is the
    defect this whole folder exists to kill, so those names are withheld and the
    absence is logged.

    DERIVED, not enumerated. An earlier revision spelled the three names out with a
    comment saying they were "``agent._MANAGED_MCP_SERVERS`` minus the control
    plane" -- and a hand-copy of a subtraction drifts in the bad direction here: a
    server added to the managed set later would miss this one, mount, and answer
    ``not_bound``, reintroducing by omission the very defect above.

    The managed set is read from :mod:`kiro_crew.mcp_cleanup`, which a ratchet test
    already pins equal to ``agent._MANAGED_MCP_SERVERS`` and which imports nothing
    heavier than ``config.paths`` -- so this leaf stays off ``agent``'s import graph
    without spelling the names again, exactly as ``acp.kas_agents`` reads it.
    """
    return frozenset(KIROCREW_BIN_MCP_SERVERS) - frozenset(CONTROL_PLANE_SERVERS)


def withheld_servers(restricted: frozenset[str]) -> frozenset[str]:
    """Every server name an ``mcpServers`` array must not mount for a session.

    ONE owner for the question, because two consumers ask it: the projection, and
    the pooled-broker append. A stub the shared MCP gateway wraps carries the SAME
    name as the entry it rewrites, so a name withheld from the projection and then
    re-added as a stub is un-withheld -- and the stub is the UNRESTRICTED server,
    which is the worse of the two.

    Two reasons a name lands here, and they share a shape: this transport cannot
    deliver the thing that makes the server correct.

    * its spec narrows it per tool and the array has no per-tool deny slot
      (:func:`~kiro_crew.acp.session_mcp.session_mcp_restricted_servers`);
    * it is one of Crew's own identity-bound servers, which cannot be handed this
      session's credential on an element the spec describes
      (:func:`identity_bound_crew_servers`).

    ``restricted`` comes from the caller's own parse: a projection derives the
    translation and this set from ONE read of the spec, so a spec gaining
    ``disabledTools`` between two reads cannot yield a withhold set from the old
    bytes applied to a translation of the new ones. Resolving it here would be
    exactly that second read, which is why this takes the set and not the agent.

    Free of I/O; the caller has already paid for the parse.
    """
    return frozenset(restricted) | identity_bound_crew_servers()

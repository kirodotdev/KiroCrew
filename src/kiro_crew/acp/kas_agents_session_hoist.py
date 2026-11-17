"""Carry the active agent's managed MCP servers in a KAS session's own ``mcpServers`` array.

:func:`hoist_servers_for_session` moves Crew's managed (and builtin app) declarations
of the ACTIVE agent out of its projected ``customAgents`` entry into the session-level
array, stamps each with the session's identity token on a re-derived managed launch,
and puts Crew's own servers first. Its docstring says what is hoisted and why.

:mod:`kiro_crew.acp.kas_agents` imports every name back and serves this module as an
owner of its mirror (:mod:`kiro_crew._facade_mirror`), so it stays the import path and
the patch surface callers and tests use. Logs under that module's logger.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from kiro_crew.acp.kas_agents_mcp_servers import MANAGED_MCP_SERVER_NAMES
from kiro_crew.acp.kas_app_identity import TOKENED_ENV_KEYS_KEPT as _TOKENED_ENV_KEYS_KEPT
from kiro_crew.acp.kas_app_identity import tokened_app_element, without_noop_restrictions
from kiro_crew.acp.kas_legacy_negotiation import _pin_legacy_negotiation

logger = logging.getLogger("kiro_crew.acp.kas_agents")


#: The keys a projected stdio declaration may carry and still be reproduced
#: exactly by :func:`kiro_crew.acp.session_mcp.acp_server_element`. Anything
#: else on a managed entry is a user customization with no session-level
#: carrier -- ``timeout``, for one -- so an entry carrying one stays in the
#: block. ``disabledTools`` never reaches here: the projection carries it in the
#: agent's ``excludedTools`` and drops it from the entry. ``disabled`` is absent from that list of
#: examples for a reason worth stating: a disabled entry never reaches this
#: function, because :func:`_project_mcp_servers` declines to declare it at all.
_HOISTABLE_ENTRY_KEYS = frozenset({"command", "args", "env", "type"})


def _managed_first(servers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """*servers* with Crew's own entries moved to the front, each group in its order.

    KAS starts a session's servers in array order, a few at a time, and a server
    that never answers ``initialize`` holds its place until the engine gives up on
    it. Session start waits for Crew's servers (``wait_mcp_ready``), so behind slow
    user servers they would delay the first prompt for nothing. Only the start
    order moves: the same entries are sent.
    """

    def is_managed(element: Any) -> bool:
        # The same ownership test as _pin_legacy_negotiation: a same-named entry
        # with no command (a remote server) is not Crew's launch.
        return (
            isinstance(element, dict)
            and element.get("name") in MANAGED_MCP_SERVER_NAMES
            and bool(element.get("command"))
        )

    return [e for e in servers if is_managed(e)] + [e for e in servers if not is_managed(e)]


def hoist_servers_for_session(
    custom_agents: list[dict[str, Any]] | None,
    agent_id: str,
    session_servers: list[dict[str, Any]],
    session_token: str = "",
    *,
    managed_entries: Mapping[str, Any] | None = None,
    app_launches: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Carry the ACTIVE agent's servers for this session in the session-level array.

    *app_launches* (from :func:`kas_app_identity.resolve_app_identity_launches`)
    names the builtin app servers that run Crew's own code. Each is hoisted like a
    managed server and, with a token, relaunched from its shipped manifest launch
    and stamped (:func:`kas_app_identity.tokened_app_element`).

    Only managed declarations move; user-declared ones stay in the block on
    ``session/new`` and ``session/load`` alike, where KAS launches them and reads
    their ``timeout`` (a session-level element has no ``timeout`` slot, so it
    would get KAS's 60 s).

    Captured released kiro-cli 2.18.0 honours a session-level ``mcpServers``
    entry over a same-named global or workspace ``mcp.json`` server on
    ``session/new`` and ``session/load`` alike. The array stamps no status
    provenance. The
    retained 2.20.0 capture proves a session-level injection reports
    ``origin: client`` and reaches readiness; its same-name collision behaviour
    was not probed. The session-level array is therefore the one declaration site
    whose connected report is positively the session's own on both, and Crew's
    managed servers -- the ones whose env carries this session's key -- belong
    there rather than in the block, exactly as the member dispatch server already
    travels.

    Pure and non-mutating: returns a new agents list (the active descriptor
    shallow-copied with the hoisted names removed from ``mcpServers``) and a new
    array (Crew's own servers first, then the caller's other entries, then the
    other hoisted elements by name). The
    entries hoisted are the ALREADY projected ones -- credential fields withheld,
    ``autoApprove`` dropped, ``KIROCREW_PORT``/``KIROCREW_SESSION_KEY`` applied
    -- so no spec is re-read and the source snapshot is untouched.

    What is NOT hoisted, each deliberately:

    * a name the caller's array already carries -- a broker stub or the member
      dispatch entry is authoritative and a name must appear once;
    * a non-managed server -- third-party declarations are not this seam's;
    * an inactive agent's block -- it must not widen the active session's tool
      surface or carry another identity's key;
    * an entry with a key outside :data:`_HOISTABLE_ENTRY_KEYS`, a ``type``
      other than ``stdio``, or no usable command -- a restriction, a registry
      marker or a malformed entry keeps the block path, where it is honoured.

    A managed entry such a key keeps in the block still launches the managed
    invocation: its ``command`` and ``args`` come from *managed_entries*
    (:func:`~kiro_crew.agent.with_managed_launch`) and every other key stays as
    projected, so a user spec copied from ``kirocrew.json`` does not start the
    payload an upgrade has since removed. It gets no session token.

    *session_token* is this session's signed identity token, and every hoisted
    element carries it (``KIROCREW_STUB_SESSION_TOKEN``). The runtime stamps the
    token onto its session-level array BEFORE this hoist runs, so the elements
    added here would otherwise be the only managed servers launched without it:
    each one reads the session's tool policy behind an attestation, and without
    the token ``kirocrew-core`` comes up present-but-unusable, refusing every call
    as ``identity_unattested``. Only the hoisted elements receive it -- a caller
    entry already carries its own identity, and a third-party server is not
    Crew's to hand a session credential. Empty leaves the elements as projected.

    A tokened element never launches what the spec says. KAS spawns the hoisted
    element itself, so ``gatewayd._spawns_own_control_plane`` -- the check that
    refuses the token to any binary that is not the managed one -- never sees it,
    and a hand-edited spec naming another program ``kirocrew-core`` would
    otherwise be handed this session's identity. So the launch is re-derived from
    :func:`~kiro_crew.agent.managed_mcp_spec_entry` (``include_opt_in=True``, the
    form the daemon compares against), exactly as
    ``mcp_gateway.rewriter._repair_control_plane_entry`` and the native-mount
    path in :mod:`kiro_crew.acp.session_mcp` do. The env is composed outward
    too: only ``KIROCREW_PORT`` / ``KIROCREW_SESSION_KEY``, which the projection
    set from the gateway's own values, then the managed env pinned last; the
    spec's ``KIROCREW_HOME`` does not survive onto a tokened launch. A name
    whose managed invocation does not resolve is still hoisted as projected,
    WITHOUT the token -- the daemon would deny it anyway, and a session with an
    unattested control plane is better than one with none.

    *managed_entries* is the managed invocation per name, already resolved by
    :func:`resolve_managed_entries` off the event loop (the KAS harness's
    ``session_extras`` thread). Resolving it here would run the launcher's
    ``is_file`` / ``os.access`` / ``shutil.which`` probes on the loop at every
    session start. ``None`` resolves in place, for a caller that is
    already off the loop.

    The agent's ``tools`` / ``excludedTools`` / ``permissions`` are untouched:
    ``@server`` refs resolve wherever the server was declared, which is the same
    property ``member_dispatch`` already relies on for ``@kirocrew-dashboard``.

    Every managed element in the returned array, caller entries included, is
    pinned to legacy version negotiation (:func:`_pin_legacy_negotiation`) and
    placed ahead of every other entry (:func:`_managed_first`), because KAS
    starts servers in array order and session start waits for Crew's own.
    """
    if not custom_agents:
        # The kiro path (no wire payload) returns here without importing the
        # agent/config translation machinery below as a side effect.
        return custom_agents, session_servers
    from kiro_crew.acp.session_mcp import acp_server_element
    from kiro_crew.agent import with_managed_launch

    apps = app_launches or {}
    taken = {
        str(entry.get("name"))
        for entry in session_servers
        if isinstance(entry, dict) and entry.get("name")
    }
    hoisted: list[dict[str, Any]] = []
    out_agents: list[dict[str, Any]] = []
    relaunched_in_block = False
    for descriptor in custom_agents:
        declared = descriptor.get("mcpServers") if descriptor.get("id") == agent_id else None
        if not isinstance(declared, dict):
            out_agents.append(descriptor)
            continue
        remaining: dict[str, Any] = {}
        relaunched = False
        for name, entry in declared.items():
            keep = True
            managed = name in MANAGED_MCP_SERVER_NAMES or name in apps
            if name in apps:
                # The shipped manifest declares ``disabled: false``; the resolver
                # already refused a restricted entry, so only no-op keys drop here.
                entry = without_noop_restrictions(entry)
            if (
                managed
                and name not in taken
                and isinstance(entry, dict)
                and set(entry) <= _HOISTABLE_ENTRY_KEYS
                and entry.get("type", "stdio") == "stdio"
            ):
                element = acp_server_element(name, entry)
                if element is not None:
                    hoisted.append(element)
                    taken.add(name)
                    keep = False
            if not keep:
                continue
            if (
                name in MANAGED_MCP_SERVER_NAMES
                and name not in taken
                and isinstance(entry, dict)
                and "command" in entry
            ):
                # KAS launches a block entry as declared, so a restriction that
                # keeps it here must not also keep a launcher an upgrade removed.
                # A name the caller's array carries is launched from there.
                kept = with_managed_launch(entry, _managed_entry(name, managed_entries))
                relaunched = relaunched or kept is not entry
                entry = kept
            remaining[name] = entry
        if len(remaining) == len(declared) and not relaunched:
            out_agents.append(descriptor)
            continue
        relaunched_in_block = relaunched_in_block or relaunched
        copied = dict(descriptor)
        if remaining:
            copied["mcpServers"] = remaining
        else:
            del copied["mcpServers"]
        out_agents.append(copied)
    if not hoisted:
        agents = out_agents if relaunched_in_block else custom_agents
        return agents, _pin_legacy_negotiation(_managed_first(session_servers))
    hoisted.sort(key=lambda element: element["name"])
    if session_token:
        # Deferred like session_mcp above: the kiro path returns before this, and
        # the token is per SESSION while the projection is per agent.
        # Only managed and app entries are hoisted, and each is tokened below.
        from kiro_crew.mcp_gateway.session_servers import attach_stub_session_token

        tokened: list[dict[str, Any]] = []
        for element in hoisted:
            name = element["name"]
            if name in MANAGED_MCP_SERVER_NAMES:
                element = _tokened_managed_element(
                    element, session_token, attach_stub_session_token, managed_entries
                )
            elif name in apps:
                element = tokened_app_element(
                    element, apps[name], session_token, attach_stub_session_token
                )
            tokened.append(element)
        hoisted = tokened
    return out_agents, _pin_legacy_negotiation(_managed_first([*session_servers, *hoisted]))


def _managed_entry(name: str, managed_entries: Mapping[str, Any] | None) -> Any:
    """*name*'s managed invocation: the one resolved off the loop, else resolved here."""
    if managed_entries is not None and name in managed_entries:
        return managed_entries[name]
    from kiro_crew.agent import managed_mcp_spec_entry

    return managed_mcp_spec_entry(name, include_opt_in=True)


def _tokened_managed_element(
    element: dict[str, Any],
    session_token: str,
    attach: Any,
    managed_entries: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """*element* relaunched as the managed invocation and stamped with the token.

    ``None`` from the managed source (name not managed, closed ``spec_gate``,
    unresolvable invocation) returns *element* untouched and untokened: that is
    the verdict ``gatewayd._spawns_own_control_plane`` reaches for the same name,
    so nothing here hands out a token the daemon would refuse.
    """
    from kiro_crew.agent import _managed_mcp_env

    name = str(element.get("name", ""))
    managed = _managed_entry(name, managed_entries)
    if not isinstance(managed, dict) or not managed.get("command"):
        logger.warning(
            "hoisted MCP server %r gets no session token: its managed invocation does"
            " not resolve, so the launch the spec declares cannot be attested",
            name,
        )
        return element
    managed_cmd = str(managed["command"])
    managed_args = [str(a) for a in managed.get("args", [])]
    declared_args = element.get("args")
    if element.get("command") != managed_cmd or declared_args != managed_args:
        # Neither value is logged: the declared args may carry a secret, and the
        # managed invocation is enough to find the entry.
        logger.warning(
            "hoisted MCP server %r declares a launch that is not the managed"
            " invocation; launching the managed one so the session token only"
            " reaches Kiro Crew's own binary",
            name,
        )
    raw_env = element.get("env")
    pairs: list[Any] = raw_env if isinstance(raw_env, list) else []
    env = {
        str(pair["name"]): str(pair.get("value", ""))
        for pair in pairs
        if isinstance(pair, dict) and pair.get("name") in _TOKENED_ENV_KEYS_KEPT
    }
    env.update(_managed_mcp_env())
    relaunched = dict(element)
    relaunched["command"] = managed_cmd
    relaunched["args"] = managed_args
    relaunched["env"] = [{"name": k, "value": v} for k, v in env.items()]
    (stamped,) = attach([relaunched], session_token)
    return stamped

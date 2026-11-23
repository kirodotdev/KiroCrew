"""The ``mcpServers`` block a KAS ``ClientCustomAgent`` declares for one agent spec.

:func:`_project_mcp_servers` decides which of the spec's servers KAS is told about and
what each entry carries over the wire: a muted, gated-off or registry-governed entry is
not declared, a stubbed name is left to the session-level injection, and every entry
loses ``autoApprove`` and the credential-bearing fields Crew does not relay. Its
docstring gives the rule for each subtraction.

:mod:`kiro_crew.acp.kas_agents` imports every name back and serves this module as an
owner of its mirror (:mod:`kiro_crew._facade_mirror`), so it stays the import path and
the patch surface callers and tests use. Logs under that module's logger.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew import mcp_cleanup
from kiro_crew.acp import kas_engine_features as engine
from kiro_crew.acp import kas_env_launch, session_label
from kiro_crew.acp.kas_app_identity import gateway_bound_port
from kiro_crew.acp.kas_env_withhold import _withhold_credential_fields
from kiro_crew.acp.kas_projection_reads import (
    _gated_off_managed_servers,
    _registry_governed,
    _relayed_env_references,
)
from kiro_crew.acp.kas_tool_limits import (
    _KAS_CARRIED_ELSEWHERE,
    _KAS_DISCARDED_ENTRY_KEYS,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.mcp_cleanup import (
    KIROCREW_BIN_MCP_SERVERS,
    MCP_REGISTRY_TYPE,
    mcp_entry_is_registry_governed,
    mcp_server_muted_in_any_store,
)
from kiro_crew.mcp_oauth_projection import registry_withheld

logger = logging.getLogger("kiro_crew.acp.kas_agents")

#: Kiro Crew's OWN managed MCP servers — the ones whose ``env`` may retain a
#: single Crew-authored key through projection (see :func:`_project_mcp_servers`).
#:
#: Imported from :mod:`kiro_crew.mcp_cleanup`, which already pins this set to
#: ``agent._MANAGED_MCP_SERVERS`` with a ratchet test and imports nothing heavier
#: than ``config.paths`` — so this projection leaf stays off the config-loader /
#: aiohttp import chain without spelling the four names a third time.
MANAGED_MCP_SERVER_NAMES = frozenset(KIROCREW_BIN_MCP_SERVERS)

#: Fields that carry a secret. ``env`` "routinely holds tokens and API keys"
#: (``mcp_gateway.session_servers``) and a remote entry's ``headers`` can hold a
#: static ``Authorization`` value, so neither crosses the wire intact.
_CREDENTIAL_BEARING_FIELDS = ("env", "headers")

#: Per server: the withheld ``env`` key names, the fields anything went from, and
#: whether it is a stdio server (one with a ``command``, the kind the gateway routes).
#: Two field values say the server does not run at all rather than run without a
#: setting: :data:`NOT_RUN_REGISTRY` and :data:`NOT_RUN_INSECURE_URL`.
WithheldOut = dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]]

#: Registry mode is on, so Crew withholds a server it did not author.
NOT_RUN_REGISTRY = "registry"
#: One of Crew's own servers in registry mode: declared, and dropped by KAS only
#: when the Kiro profile is registry-governed (KAS reads that mode itself).
NOT_RUN_REGISTRY_IF_GOVERNED = "registry-if-governed"
#: A plain ``http`` url to a non-loopback host, which KAS refuses.
NOT_RUN_INSECURE_URL = "insecure-url"


#: Crew-internal bookkeeping on a rewritten entry. Never belongs on the wire: an
#: unknown field can fail a strict schema, and it means nothing to the backend.
_WRAPPER_MARKERS = ("_kirocrew_mcp_gateway_wrapped", "_mc_mcp_gateway_wrapped")


def _project_mcp_servers(
    spec: dict[str, Any],
    agent_id: str,
    stub_server_names: frozenset[str],
    session_key: str = "",
    withheld_out: WithheldOut | None = None,
    launch_key: str = "",
    engine_features: frozenset[str] = frozenset(),
    forward_ssh_auth_sock: bool = False,
    scope_muted: frozenset[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """The spec's ``mcpServers``, minus stubbed names and minus two field classes.

    *launch_key* names the local launch snapshots (:mod:`kas_env_launch`); it is
    *session_key* unless the caller passes one. With neither, nothing is relayed.
    *forward_ssh_auth_sock* is whether the engine's own spawn kept the gateway's
    ssh-agent socket, which alone decides whether a launched server gets it.
    *engine_features* (:mod:`kas_engine_features`) sends ``oauth`` and registry
    pointers to an engine that announced it accepts them.

    *withheld_out*, when given, maps each declared server Crew did not author that
    lost something to the credential filter onto ``(env key names, fields, stdio)``. Names
    only, never a value, for :mod:`kiro_crew.acp.kas_withheld_notice`.

    Six subtractions, each load-bearing:

    * **stubbed names** — those arrive as the session-level ``mcpServers`` param,
      which outranks an agent-declared entry. Emitting both is the double
      registration this block exists to avoid. Applied LAST: the withholds below
      are decisions about whether the server may run at all, and a name handed to
      the injection escapes every one of them.
    * **``autoApprove``** — an auto-approved MCP tool is approved by the host and
      emits no permission request, so ``hooks.on_tool_call`` (the always-on deny
      floor, the sensitive-path check, the governance ceiling) never runs for it.
      ``agent.py`` states the rule for Crew's own servers ("DELIBERATELY NO
      ``autoApprove`` KEY, and none may ever be added"); relaying one copied from
      a spec would grant through this path what that rule refuses on the other,
      and on KAS there is no wire slot for hooks at all. Auto-approve reaches KAS
      only as ``permissions``, derived from the ceiling-filtered ``allowedTools``;
      the verbs the kiro-cli route keeps join that input in
      :func:`to_client_custom_agent` (:mod:`kiro_crew.acp.kas_auto_approve`).
    * **``env`` and ``headers``** — projection puts these on the wire, and a
      declared server's env routinely holds tokens. Every server is filtered; the
      classes differ only in what survives. A server Crew did not author loses
      ``headers`` and every env value except a reference to a name the edition
      relays (:data:`_ENV_REFERENCE`), which carries no value, and a boolean
      literal :func:`_relayable_literal` allows. One of Crew's
      OWN managed servers additionally keeps ``KIROCREW_HOME``, because that key is the only
      reason managed env is projected at all: without it the shims read a
      different data home than the gateway. A managed entry still lives in a
      user-editable agent file, so a hand-added literal under a managed name is
      withheld like any other. An opted-in entry gains only the session label.

    * **a muted server** — an entry carrying ``disabled: true`` is not declared at
      all. KAS's wire schema accepts the field and its mapper then drops it
      (:data:`_KAS_DISCARDED_ENTRY_KEYS`), so projecting a muted entry launches
      the very server the user silenced. Omitting the declaration is the only
      Crew-side way to say "do not launch this" that the backend cannot discard,
      and it says the same thing: a server KAS was never told about does not run.
      The mute is the user's own decision about a server they can un-mute, so
      honouring it costs no capability — unlike the credential strip above, it has
      no residue.
    * **a managed server whose ``spec_gate`` is closed** (``kirocrew-computer``
      while Computer Use is off or unsupported). Both spec writers withhold the
      entry, but a spec they did not write (a legacy or hand-written agent file)
      can still declare it. Declared, the engine launches a backend that
      advertises no tools, and the readiness barrier waits for every managed
      server it was sent (``mcp_session_report.required_managed_servers``), so
      the session fails on the timeout. The gate is read with the writers' own
      fail-closed snapshot (:func:`_gated_off_managed_servers`); the
      ``@server`` ref in ``tools`` stays, as on a spec Crew writes.
    * **a registry-governed entry** — see :func:`_registry_governed`. The filter
      mirrors kiro-cli's, which is SYMMETRIC: in registry mode an entry survives
      only by resolving its ``"type": "registry"`` marker against the admin's
      catalog, and OUTSIDE registry mode a marked entry is the one that is
      dropped. So a non-managed entry is withheld when registry mode is on
      (marked or not) and also when it is marked while the mode is off. Both
      cases are ones KAS would get wrong rather than merely differently: it has no
      slot for ``type``, so it sees every entry as unmarked — dropping all of them
      under an admin's catalog, and mounting a marked one outside it that kiro-cli
      would have dropped. Withholding here makes the first case diagnosable and
      the second correct.

    Crew's OWN managed servers are exempt from that filter and keep their marker,
    which is the same exemption :mod:`kiro_crew.acp.session_mcp` makes for the
    control plane and for the same reason: they are the host's own processes, and
    a session that loses them cannot report back to its channel at all. On today's
    KAS the marker does not reach the registry filter, so under registry mode the
    host drops them anyway and the session comes up with no Crew tools — which is
    why that case is WARNED about once rather than left silent, and why the
    exemption is still right: the day the wire carries ``type``, a governed
    session keeps its control plane with no further change here.

    A non-managed server therefore starts with its boolean flags but without any
    other literal, and may fail to authenticate or miss a setting. The drop is logged with KEY NAMES
    ONLY so an operator can see why, without the value reaching a log.

    Hoisting the entry into the session-level array would not keep its env
    local: :func:`hoist_servers_for_session` puts it in ``session/new``'s own
    ``mcpServers``, the same wire, and hoists the entry as projected here. The
    filter is therefore the one place the env is decided.

    Note what this canNOT reach: ``command``, ``args`` and ``url`` are how the
    server is launched or addressed, so a secret embedded THERE (an ``--api-key``
    argv, a signed query string) still crosses the wire. Stripping them would not
    withhold a credential, it would unmake the server — the exact "declared but
    absent" state this function exists to end — so the residue is accepted and
    stated rather than papered over.
    """
    from kiro_crew import agent as agent_facade

    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return {}

    out: dict[str, dict[str, Any]] = {}
    registry_mode = _registry_governed()
    gated_off = _gated_off_managed_servers()
    relayed = _relayed_env_references()
    warned_about_the_marker = False
    launch_key = launch_key or session_key
    # Every scope read once for the whole projection, not per server: the caller's
    # reading, else this pass's, through the one reader (and patch seam).
    if scope_muted is None:
        scope_muted = mcp_cleanup.mcp_scope_muted_aliases()
    for name, entry in servers.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        managed = name in MANAGED_MCP_SERVER_NAMES
        if mcp_server_muted_in_any_store(name, entry, scope_muted=scope_muted):
            # ``mcp_server_muted_in_any_store`` is ``mcp_entry_is_muted`` plus the config scopes
            # (why, for a spec that DECLARES the server: see it), so this stays the shared
            # launch-decision reading and the gateway rewriter still cannot wrap an entry this
            # function would withhold -- a wrapped name arrives as a stubbed name and is subtracted
            # below, before any check here. A non-boolean is not forwarded either: ``disabled:
            # z.boolean()`` fails the wire schema, and a client agent that fails it is dropped WHOLE
            # (``validateAndConvertClientCustomAgents`` logs ``client.agent.drop.invalid``), costing
            # the session every Crew tool. Arm two tests ``in``, because ``get`` reads an absent key
            # and a ``null`` alike and only the second mutes; arm three means a scope did.
            disabled = entry.get("disabled")
            if disabled is True:
                why = (
                    "the entry is disabled, and the customAgents wire schema accepts "
                    "that flag and then discards it, so a declared entry would launch "
                    "the server anyway"
                )
            elif "disabled" in entry and disabled is not False:
                why = (
                    "its 'disabled' value is not a boolean, so it is read as a mute "
                    "rather than coerced; forwarding it would fail the wire schema and "
                    "cost the session the whole injected agent"
                )
            else:
                why = "an MCP config scope disables it, so the user switched it off"
            logger.info("agent %r: not declaring MCP server %r — %s", agent_id, name, why)
            continue
        if name in gated_off:
            logger.info(
                "agent %r: not declaring MCP server %r: its spec_gate is closed, so a "
                "fresh spec would not declare it, and its backend would advertise no "
                "tools and hold the session's MCP readiness until it times out",
                agent_id,
                name,
            )
            continue
        if (
            engine.MCP_REGISTRY_POINTERS in engine_features
            and not managed
            and mcp_entry_is_registry_governed(entry)
        ):
            # User servers only. Crew's own stay stdio so the session-level hoist
            # still stamps their session token; a pointer has no slot for it.
            out[name] = engine.registry_pointer(entry)
            continue
        if registry_mode and withheld_out is not None:
            # Crew withholds every server it did not author in registry mode. Its
            # own are declared, and KAS drops them only if the Kiro profile it
            # reads is registry-governed too, so the notice says so conditionally.
            reason = NOT_RUN_REGISTRY_IF_GOVERNED if managed else NOT_RUN_REGISTRY
            withheld_out[name] = ((), (reason,), bool(entry.get("command")))
        if registry_withheld(name, entry, registry_mode):
            logger.info(
                "agent %r: withholding MCP server %r — %s",
                agent_id,
                name,
                (
                    "registry mode is on and the wire schema has no slot for the "
                    "registry marker, so the host's catalog filter drops this "
                    "entry whether or not it is marked"
                    if registry_mode
                    else "registry mode is off and the entry carries the registry "
                    "marker, so kiro-cli drops it too"
                ),
            )
            continue
        if managed and registry_mode and not warned_about_the_marker:
            # Deliberately NOT conditional on the entry carrying the marker. A
            # spec materialized while the mode was off has unmarked managed
            # entries, and that install loses its control plane in exactly the
            # same way — requiring the stamp would keep the one case that cannot
            # self-diagnose silent.
            warned_about_the_marker = True
            logger.warning(
                "agent %r: registry mode is on, and this backend's customAgents "
                "wire schema has no slot for the %r marker Crew stamps on its own "
                "MCP servers. The host therefore sees them as unmarked and its "
                "catalog filter drops them, so this session starts with no Crew "
                "control plane: spawn_run, cron_add, learn_add, artifacts, "
                "knowledge and monitoring are all absent, with no error from the "
                "host. Nothing on this side can carry the marker; the workaround "
                "is `kirocrew config set agent.mcp_registry_mode false` on an "
                "install whose profile is not actually registry-governed.",
                agent_id,
                MCP_REGISTRY_TYPE,
            )
        if name in stub_server_names:
            # LAST of the subtractions, deliberately. A stubbed name is declared by
            # the session-level injection instead of here, and that path answers
            # none of the questions above -- so a name subtracted before them is a
            # server admitted without them. The gateway declines to wrap a muted or
            # catalog-governed entry, which keeps such a name out of this set in the
            # first place; this ordering is what makes the answer here true on its
            # own rather than by trusting that. It is the order
            # :func:`kiro_crew.acp.session_mcp.session_mcp_servers` uses.
            continue
        projected = {k: v for k, v in entry.items() if k not in _WRAPPER_MARKERS}
        projected.pop("autoApprove", None)
        labelled, relay_env = session_label.take_kas_marker(projected, entry.get("env"), managed)
        for key in _KAS_CARRIED_ELSEWHERE:
            # Carried in the agent's ``excludedTools`` (``to_client_custom_agent``),
            # so the entry drops it: left on, it would keep a managed server off
            # the session-level array and so without its session token.
            projected.pop(key, None)
        # Always, whatever the engine announces: 0.66.104 reads the block, and
        # older engines drop it without failing the server (kas_engine_features).
        engine.carry_oauth(projected, agent_id=agent_id, name=name, logger=logger)
        discarded = [k for k in _KAS_DISCARDED_ENTRY_KEYS if projected.get(k)]
        if discarded:
            # Read off the PROJECTED entry, not the spec's: a key this function
            # already removed never reaches the backend, so naming it here would
            # report Crew's own subtraction as the backend's. Debug, because the
            # server is still declared and still runs — this explains a
            # restriction that had no effect, not a lost capability. ``disabled``
            # cannot appear: that entry returned above.
            logger.debug(
                "agent %r: MCP server %r declares %s, which the customAgents wire "
                "schema accepts and then discards, so the restriction has no "
                "effect on this backend.",
                agent_id,
                name,
                "/".join(discarded),
            )
        dropped_env: list[str] = []
        if managed:
            # The spec's own marker copies are not a withheld credential to report.
            agent_facade.drop_policy_markers(projected)
        withheld = _withhold_credential_fields(
            projected, managed=managed, relayed=relayed, dropped_env=dropped_env
        )
        if managed and projected.get("command"):
            # KAS starts managed stdio children with a minimal environment.
            agent_facade.add_policy_markers(projected)
        stdio = bool(projected.get("command"))
        if managed:
            # Native MCP children do not inherit the gateway's environment.
            # Take the live listener from the gateway, never from an editable
            # spec or process discovery.
            bound_port = gateway_bound_port()
            if bound_port:
                projected.setdefault("env", {})["KIROCREW_PORT"] = bound_port
            if session_key:
                projected.setdefault("env", {})["KIROCREW_SESSION_KEY"] = session_key
        elif session_key and (
            crew := kas_env_launch.crew_entry(
                projected, name, entry, session_key, agent_id, ACP_BACKEND_KAS
            )
        ):
            projected, withheld = crew, []
        elif launch_key:
            # The withheld env values stay on this machine: the engine is handed
            # Crew's launcher and a reference, never the values (kas_env_launch).
            relayable = kas_env_launch.withheld_env(relay_env, projected.get("env"))
            projected, launched, complete = kas_env_launch.relay_withheld_env(
                projected,
                relay_env,
                session_key=launch_key,
                agent_id=agent_id,
                server=name,
                forward_ssh_auth_sock=forward_ssh_auth_sock,
            )
            if complete:
                withheld = [w for w in withheld if not w.startswith("env")]
            if launched:
                # The launcher started the server with these values on this
                # machine, so the notice names only what is still missing.
                dropped_env = [k for k in dropped_env if k not in relayable]
            projected, bridged = kas_env_launch.relay_withheld_headers(
                projected,
                entry.get("headers"),
                session_key=launch_key,
                agent_id=agent_id,
                server=name,
            )
            if bridged:
                withheld = [w for w in withheld if w != "headers"]
        if (
            withheld_out is not None
            and projected.get("url")
            and not projected.get("command")
            and not kas_env_launch.kas_accepts_url(projected["url"])
        ):
            withheld_out[name] = ((), (NOT_RUN_INSECURE_URL,), False)
        elif withheld_out is not None and withheld and not managed:
            fields = tuple(field.split(" ", 1)[0] for field in withheld)
            withheld_out[name] = (tuple(sorted(dropped_env)), fields, stdio)
        if withheld:
            logger.info(
                "agent %r: not relaying %s for MCP server %r — the field can carry "
                "a credential. The server is still declared; it may need its "
                "credentials supplied another way.",
                agent_id,
                "/".join(withheld),
                name,
            )
        out[name] = session_label.with_kas_label(projected, session_key if labelled else None)
    return out

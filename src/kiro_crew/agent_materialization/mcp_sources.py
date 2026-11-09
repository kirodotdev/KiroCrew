"""Projecting the MCP sources a rebuild reads into the default agent spec.

A rebuild merges four sources into ``kirocrew.json`` in a fixed precedence -- app
manifests, the Kiro global ``mcp.json``, edition-contributed provider globals, and the
dashboard store ``~/.kiro/crew/mcp.json`` -- then resolves every server's command and
syncs the user-installed servers into ``tools`` / ``allowedTools``. The phases here are
that pipeline, in order; the orchestration, and the command resolver the phases share,
stay in :func:`kiro_crew.agent.rebuild_agent_config`.

App ownership is read from the manifests that mint the keys
(:func:`_app_owned_mcp_keys`), never inferred from a colon in a name.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

from kiro_crew import agent as agent_mod
from kiro_crew import user_json
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_materialization import auto_approve, mcp_aliases
from kiro_crew.env import (
    MCP_PATH_HINT,
    dedup_path,
    describe_search_path,
    emit_env,
    expand_spec_args,
)
from kiro_crew.mcp_cleanup import (
    invalid_disabled_flag,
    mcp_entries_muted,
    mcp_entry_is_muted,
    mcp_entry_is_registry_governed,
    warn_invalid_disabled,
)
from kiro_crew.mcp_gateway.secret_uri import routed_for_secret_reference, secret_reference_keys
from kiro_crew.mcp_provenance import (
    DERIVED_KEY,
    WITHHELD_KEY,
    command_is_ours,
    is_withheld_mute,
    record_derived,
    recorded_source,
    source_view,
    without_marker,
)
from kiro_crew.mcp_utils import kiro_oauth_wire_entry, mcp_server_alias, without_mcp_refs
from kiro_crew.platform import safe_context_call


def _extra_mcp_scope_globals() -> list[Path]:
    """Provider-global MCP config files contributed by the edition (CPP seam).

    Mirrors ``mcp_discovery._extra_scope_sources`` and the ``/api/mcp/apply``
    uninstall path, so a companion's provider global (e.g. Claude Code's
    ``~/.claude.json``) is merged ONLY when that edition contributes it. The Default
    returns ``[]``: a server the dashboard cannot see is never re-merged. Fails closed
    to no extra scopes.
    """
    scopes: list = safe_context_call(
        lambda: list(agent_mod.current_context().mcp_tooling.extra_mcp_scopes()),
        fallback_factory=list,
        log_message="extra_mcp_scopes lookup failed; rebuild using core scopes only",
    )
    return [s.global_json for s in scopes]


def _collect_app_mcp_servers(*, audit: bool = True) -> dict[str, Any]:
    """MCP servers contributed by ENABLED apps, keyed ``{app}:{server}``.

    App MCP servers are registered straight into this agent config, not into the
    shared ``~/.kiro/settings/mcp.json``: Kiro IDE and every other kiro-cli agent read
    that file, so an app's private tools would leak into surfaces that never installed
    the app. Kiro Crew sessions read only the agent config (``includeMcpJson`` is
    pinned False).

    The manifests are therefore the authoritative source, re-derived on every rebuild,
    so a ``clean=True`` rebuild (which ignores the existing config) keeps them.

    Never raises — a broken app manifest must not stop the agent config from
    being written, or a single bad app would take down every session.
    """
    servers: dict[str, Any] = {}
    try:
        # Imported lazily: kiro_crew.apps imports back into agent/security, so a
        # module-level import here would close a cycle.
        from kiro_crew.apps.bridges import registered_app_mcp_servers
        from kiro_crew.apps.manager import get_app_manifest, is_app_enabled, list_apps
    except Exception:  # noqa: BLE001 — apps subsystem unavailable
        return servers

    try:
        apps = list_apps()
    except Exception:  # noqa: BLE001
        return servers

    # The LIVE registered map, not the manifest, is the source of truth for the
    # spec: for a `backend.port:"auto"` app the manifest port is ILLUSTRATIVE, and
    # reregister_app_mcp_servers writes the resolved URL once the backend starts.
    # Copying the manifest port would point kiro-cli at a dead port. The manifest
    # is the fallback only for a stdio/command server.
    registered = registered_app_mcp_servers()

    for app in apps:
        name = app.get("name") if isinstance(app, dict) else None
        if not name:
            continue
        try:
            if not is_app_enabled(name):
                continue
            manifest = get_app_manifest(name)
            if not manifest or not manifest.mcpServers:
                continue
            for server_name, spec in manifest.mcpServers.items():
                if not isinstance(spec, dict):
                    continue
                ref = f"{name}:{server_name}"
                live = registered.get(ref)
                if isinstance(live, dict):
                    chosen = dict(live)  # resolved live-port URL / pinned command
                elif spec.get("url"):
                    # With backend.entryPoint set the gateway launches the backend
                    # and the manifest port is illustrative, so with no live entry
                    # the server is skipped rather than written with a dead port
                    # (as _register_mcp_servers does). A SELF-MANAGED HTTP server
                    # (no entryPoint, fixed port) never gets a live registration,
                    # so its manifest URL is authoritative and kept.
                    if manifest.backend.entryPoint:
                        continue
                    chosen = dict(spec)
                else:
                    chosen = dict(spec)  # stdio/command: nothing to resolve
                servers[ref] = auto_approve._ceiling_filtered_spec(ref, chosen, audit=audit)
        except Exception:  # noqa: BLE001 — one bad app must not poison the rest
            agent_mod.logger.warning("Skipping MCP servers for app %s (manifest error)", name)
            continue
    return servers


def _app_owned_mcp_keys() -> dict[str, bool]:
    """Every ALIAS an INSTALLED app declares mapped to its enablement.

    Read from the manifests that mint the keys, because a colon prefix is not
    ownership. ``mcp_server_alias`` returns a slash-free name unchanged, so a
    global ``mcp.json`` server keyed ``npm:foo`` reaches the agent config with its
    colon intact, and an installed app may also be called ``npm`` without ever
    declaring a server called ``foo``. Attributing by prefix hands that unrelated
    server the app's enablement answer.

    Keyed by the ALIAS of the composite, because that is the identity the emitted
    ``@ref`` carries and the one a candidate is spelled with. A manifest may
    declare a slashed name -- the npm-scoped ``@playwright/mcp``, or the registry
    ``namespace/name`` form -- and ``{app}:@playwright/mcp`` then reaches the
    config as ``playwright-mcp``. A raw composite key matches no candidate at all,
    so the owner of exactly the names that NEED aliasing would read as unowned.

    Each key is a base alias, and a colliding pair collapses to one entry whose
    value is the AND of their enablements. The concrete ``base-<n>`` a collision
    mints depends on the live server map, so the ref reconcile treats family
    membership as a guess: a suffixed sibling inherits nobody's answer, its grant
    goes unless a source vouches for it, and only its mount survives.

    Covers DISABLED apps deliberately: a switched-off app's grant must not linger on
    its name. The manifest is the reader because a disabled app has no live
    registration.

    Enablement is read through :func:`app_enabled_state`, not
    :func:`is_app_enabled`, whose single False cannot tell "unreadable" from
    "switched off"; acting on the wrong one revokes a grant that nothing re-derives.

    Never raises.
    """
    owned: dict[str, bool] = {}
    try:
        # Imported lazily to avoid the import cycle (see _collect_app_mcp_servers).
        from kiro_crew.apps.manager import (
            app_enabled_state,
            get_app_manifest,
            list_apps,
        )
    except Exception:  # noqa: BLE001 — apps subsystem unavailable
        return owned
    try:
        apps = list_apps()
    except Exception:  # noqa: BLE001 — an unreadable registry claims nothing KNOWABLE
        return owned
    for app in apps:
        name = app.get("name") if isinstance(app, dict) else None
        if not name:
            # Keys are minted FROM the name, so a nameless row owns nothing. Not
            # flagged as unreadable, or one malformed row would narrow every
            # rebuild's exemption.
            continue
        try:
            _state = app_enabled_state(name)
        except Exception:  # noqa: BLE001 — an unreadable switch is not a switch-off
            _state = None
        if _state is None:
            # None is "could not read", NOT "disabled": it names no owner.
            continue
        enabled = _state
        try:
            manifest = get_app_manifest(name)
        except Exception:  # noqa: BLE001 — same answer as the None return below
            manifest = None
        if manifest is None:
            continue
        for server_name in manifest.mcpServers or {}:
            _alias = mcp_server_alias(f"{name}:{server_name}")
            # AND the answers on a collision: one base can stand for several
            # declared servers, and a `base-<n>` sibling records no claimant, so
            # the family is on only while every claiming app is (deny, not grant).
            owned[_alias] = enabled and owned.get(_alias, True)
    return owned


# Keys a scope global AUTHORS that are also TRANSPORT-INDEPENDENT, so one the
# source has since DROPPED is dropped here too. Anything else on a merged entry is
# the user's (``autoApprove``, ``disabledTools``, fields we do not model) and
# survives by being ABSENT here, so one invented later defaults to surviving.
# ``url`` is absent (a transport needs a scope that declares one;
# ``test_mcp_rebuild_reconsumption`` owns that). ``headers`` is absent too --
# reconciling it without its ``url`` would pair this source's credential with the
# entry's old endpoint. mcp.md calls this adopting as a unit. ``command``, ``args``
# and ``env`` are one such unit and are reconciled together by
# :func:`_merge_command_unit`, not here.
_SOURCE_OWNED_MCP_KEYS = ("timeout", "disabled")

# The keys that belong WITH a stdio ``command``: a server's arguments and its
# environment are written for one program. The agent entry carries that program as
# the RESOLVED absolute path the previous pass wrote (``npx`` ->
# ``/opt/node/bin/npx``), so the pairing test is the discovery module's "same
# program" comparison, not string equality.
_COMMAND_UNIT_KEYS = ("args", "env")


def _merge_command_unit(existing: dict, spec: dict) -> dict | None:
    """Project *spec*'s ``command``/``args``/``env`` onto *existing* as one unit.

    A source naming the program the entry already runs keeps the entry's RESOLVED
    command (``/…/bin/npx`` for a bare ``npx``); one that switched programs has its
    command written and the replaced unit returned, which
    :func:`resolve_mcp_servers` tries last. An entry carrying a provenance record
    is an agent-only declaration and keeps its own program: which of the two wins
    is the precedence question ``resolve_mcp_servers`` leaves open. Projected, not
    merged: a key the source dropped is removed.
    """
    source_cmd = spec.get("command")
    entry_cmd = existing.get("command")
    if not (isinstance(source_cmd, str) and source_cmd):
        return None
    if not (isinstance(entry_cmd, str) and entry_cmd):
        return None
    # Lazy: mcp_discovery is the dashboard's module and imports back into agent
    # at call time; a module-level import here would close that cycle. The
    # comparison is the one the dashboard's sync trigger uses, so the merge
    # reconciles exactly the command/env divergence the dashboard offers to sync.
    from kiro_crew.mcp_discovery import _commands_diverged

    replaced = None
    if _commands_diverged(source_cmd, entry_cmd):
        if recorded_source(existing) is not None:
            return None
        replaced = {k: existing[k] for k in ("command", *_COMMAND_UNIT_KEYS) if k in existing}
        existing["command"] = source_cmd
    for key in _COMMAND_UNIT_KEYS:
        if key in spec:
            value = spec[key]
            # Copies, for the reason merge_mcp_sources gives: the entry is
            # mutated by later passes and must never alias the source's objects.
            if isinstance(value, dict):
                value = dict(value)
            elif isinstance(value, list):
                value = list(value)
            existing[key] = value
        else:
            existing.pop(key, None)
    return replaced


def _merge_source_owned(
    mcps: dict,
    name: str,
    spec: dict,
    *,
    stale: set[str],
    replaced: dict[str, dict],
    edition_owned: bool = False,
) -> None:
    """Reconcile a scope global's *spec* onto ``mcps[name]``.

    A name the config already holds is reconciled too, so a source the user changed
    (a bumped ``timeout``) reaches the generated spec. Only a name in *stale* is
    reconciled, and reconciling RETIRES it, so a name claimed earlier in this pass by
    a higher-priority scope keeps winning. A command unit the source switched away
    from is recorded in *replaced* under *name*.

    An *edition_owned* name (one ``extra_mcp_servers()`` supplies) takes only the
    source-owned modifier keys. Its command unit (``command``, ``args``, ``env``) is
    the edition's (seeded with the entry, ``command``/``args`` refreshed by
    ``_refresh_dynamic_fields``), so a global entry can neither replace its tool
    surface nor switch its program, and the name never enters *replaced*.
    """
    existing = mcps.get(name)
    if not isinstance(existing, dict):
        mcps[name] = without_marker(spec)
        return
    if name not in stale:
        return
    stale.discard(name)
    for key in _SOURCE_OWNED_MCP_KEYS:
        if key in spec:
            existing[key] = spec[key]
        else:
            existing.pop(key, None)
    if not edition_owned:
        _previous = _merge_command_unit(existing, spec)
        if _previous is not None:
            replaced[name] = _previous


class McpSources(NamedTuple):
    """The three MCP scopes a rebuild reads, as it read them, and the managed names."""

    #: ``~/.kiro/crew/mcp.json`` -- the dashboard store, which wins a tie.
    kirocrew: dict[str, Any]
    #: ``~/.kiro/settings/mcp.json`` -- the Kiro user-level global.
    kiro_global: dict[str, Any]
    #: Every edition-contributed provider global, first scope wins.
    provider_global: dict[str, dict]
    #: The managed registry's names, which no scope may overwrite.
    managed_names: set[str]
    #: The command unit an entry ran before a scope global switched its program,
    #: by name: the resolution fallback when the new command does not resolve.
    replaced_units: Mapping[str, dict] = MappingProxyType({})

    @property
    def scopes(self) -> tuple[tuple[str, dict], ...]:
        """The scope chain in resolution priority order, labelled as the rebuild logs it."""
        return (
            ("kirocrew", self.kirocrew),
            ("kiro-global", self.kiro_global),
            ("provider-global", self.provider_global),
        )


class ResolvedServers(NamedTuple):
    """What the resolution pass withheld, and what the write binds at commit time.

    The emitted map itself is ``config["mcpServers"]``, which every later phase reads
    in place: the key normalization and the proxy purge mutate that same object.
    """

    #: Servers withheld ONLY because a declared command did not resolve here.
    unresolved: set[str]
    #: URL servers whose operator OAuth client is bound at write time -> store-owned?
    oauth_targets: dict[str, bool]
    #: ``unresolved`` with the declared command each one named, for the dashboard's
    #: record of what the latest rebuild dropped (``mcp_dropped_servers``).
    unresolved_commands: dict[str, str]

    @property
    def narrowed_away(self) -> set[str]:
        """``unresolved``, alias-spelled.

        The names THIS pass declines to emit because a declared command did not
        resolve, alias-spelled because that is the identity an emitted @ref carries
        once the alias rewrite has run. Handed to the dangling-ref reconcile at
        the end so a server that merely failed to resolve THIS time keeps its refs
        -- see prune_dangling_tool_refs for why a per-tool grant cannot be
        re-derived.

        Built from that one branch, NEVER from "declared but not in the final map":
        a commandless stub has nothing to resolve later, and a server that resolved
        here and is removed further down -- the locked app reconcile deletes an app
        entry once it cannot confirm that app is enabled -- is a real removal. Both
        would otherwise keep exactly the ref this reconcile exists to drop.
        """
        return {mcp_server_alias(_n) for _n in self.unresolved}


def merge_mcp_sources(config: dict) -> McpSources:
    """Merge the app, Kiro-global, provider-global and store MCP servers into *config*.

    Returns the scope maps as read, so the later passes resolve against the same
    reads.
    """
    managed_names = set(agent_mod._MANAGED_MCP_SERVERS)
    # Edition-supplied names: a scope global still reconciles their ``timeout``
    # and ``disabled``, never their command/args/env, the edition's invocation.
    # Not folded into ``managed_names``, which also drives the shared-server
    # tools/allowedTools/disabled sync.
    edition_names = frozenset(agent_mod._extra_mcp_servers()) - managed_names

    # App-contributed MCP servers go in FIRST so an app's namespaced entry
    # outranks any same-named leftover in the shared global file (every loop
    # below uses setdefault, so whatever lands here wins). Re-derived from the
    # enabled apps' manifests on every rebuild, which is what lets a clean
    # rebuild keep them — see _collect_app_mcp_servers for why apps don't write
    # the global file at all.
    #
    # ASSIGNMENT, not setdefault, for the app's own key: the manifest is
    # authoritative, so an `autoApprove` the ceiling just stripped must replace
    # the previous rebuild's stale grant, or those tools keep skipping the
    # PreToolUse gate.
    # Names a PREVIOUS rebuild left behind -- the only stale projections a changed
    # source reconciles. Seeded here so the app loop can retire what it claims.
    _stale = set(config.get("mcpServers", {}))
    _replaced: dict[str, dict] = {}
    for _app_srv, _app_spec in _collect_app_mcp_servers().items():
        if _app_srv not in managed_names:
            config.setdefault("mcpServers", {})[_app_srv] = _app_spec
            # The manifest just spoke, so a same-named shared-file leftover must
            # not reconcile onto it -- this is how the app entry keeps outranking.
            _stale.discard(_app_srv)
            # EXPOSE it: kiro-cli connects entries declared in `mcpServers`, but
            # an unreferenced server contributes no tools to the agent. `tools`
            # is the unconditional exposure list (the final dedup removes any
            # duplicate); auto-approve stays governed —
            # the spec's `autoApprove` was already ceiling-filtered in
            # _collect_app_mcp_servers, and the final allowedTools pass covers the
            # @ref if it ever lands there.
            config.setdefault("tools", []).append(f"@{_app_srv}")

    shared_mcp = user_json.load_mcp_servers(agent_mod._KIRO_MCP_JSON)
    for name, spec in shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy so config never aliases the source dict — a later update()
            # (kirocrew merge) must not mutate shared_mcp, which is reused as a
            # fallback candidate during command validation below. The copy also
            # drops our authorship marker: it records who wrote the entry in a
            # SHARED file and has no meaning in a spec we render ourselves, so
            # keeping it would put a key in front of the runtime that says nothing
            # to it.
            _merge_source_owned(
                config.setdefault("mcpServers", {}),
                name,
                spec,
                stale=_stale,
                replaced=_replaced,
                edition_owned=name in edition_names,
            )

    # Edition-contributed provider globals (CPP seam) rank BELOW the Kiro global:
    # a name claimed earlier in this pass was retired from ``_stale``, so these only
    # fill gaps. In OSS the seam is empty (see _extra_mcp_scope_globals).
    # ``extra_shared_mcp`` keeps the raw per-scope entries (first scope wins) for
    # the fallback-candidate lookup and the shared-server tools sync below.
    extra_shared_mcp: dict[str, dict] = {}
    for scope_global in _extra_mcp_scope_globals():
        scope_shared_mcp = user_json.load_mcp_servers(scope_global)
        for name, spec in scope_shared_mcp.items():
            if not isinstance(spec, dict):
                continue
            extra_shared_mcp.setdefault(name, spec)
            if name not in managed_names:
                # Copy (see note above) so the source dict stays pristine for
                # the fallback-candidate lookup.
                _merge_source_owned(
                    config.setdefault("mcpServers", {}),
                    name,
                    spec,
                    stale=_stale,
                    replaced=_replaced,
                    edition_owned=name in edition_names,
                )

    # ~/.kiro/crew/mcp.json overrides kiro mcp.json for the kirocrew agent —
    # kirocrew-specific config wins in a tie.
    # Uses update() to merge into existing specs, preserving user-set fields
    # like autoApprove while letting kirocrew's command/args/env win.
    # Skip managed servers for the same reason as above.
    kirocrew_mcp = user_json.load_mcp_servers(agent_mod._user_dir() / "mcp.json")
    for name, spec in kirocrew_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            mcps = config.setdefault("mcpServers", {})
            if "command" in spec and name in _replaced:
                # The store's command outranks the program a scope global just
                # switched to, so the entry gets back the args/env it ran with.
                for key in _COMMAND_UNIT_KEYS:
                    mcps[name].pop(key, None)
                mcps[name].update(_replaced.pop(name))
            if name in mcps and isinstance(mcps[name], dict):
                # mcps[name] is a private copy (globals were copied in above),
                # so update() does not mutate any source dict.
                mcps[name].update(spec)
            else:
                mcps[name] = dict(spec)
    return McpSources(kirocrew_mcp, shared_mcp, extra_shared_mcp, managed_names, _replaced)


def _mute_withheld(entry: dict) -> dict:
    """*entry* muted in place, with the record that this rebuild muted it.

    Used for a server declared ONLY in the agent spec: this file is its one copy,
    so dropping it would lose the declaration, while emitting it live would start
    it with the literal reference. A user-written ``disabled`` is left as found
    and gains no record, so it is never lifted.
    """
    if mcp_entry_is_muted(entry) and WITHHELD_KEY not in entry:
        return entry
    muted = dict(entry)
    muted["disabled"] = True
    muted[WITHHELD_KEY] = True
    return muted


def _lift_withheld_mute(entry: dict) -> dict:
    """*entry* without a mute :func:`_mute_withheld` wrote; unchanged otherwise."""
    if WITHHELD_KEY not in entry:
        return entry
    lifted = {k: v for k, v in entry.items() if k != WITHHELD_KEY}
    if lifted.get("disabled") is True:
        lifted.pop("disabled")
    return lifted


def _gateway_routed_names() -> frozenset[str]:
    """The servers the running MCP gateway starts for this agent; empty on any failure.

    Routed means BOTH: the saved routing lists the server
    (``mcp_gateway.stub_servers``), AND the overlay the running gateway built at
    its start holds a stub for it that a session is handed
    (:func:`~kiro_crew.mcp_gateway.session_servers.injection_server_names`). The
    saved list alone is only intent: the routing switch records it and asks for
    a restart (``mcp_broker._apply_mcp_stub``), and until that restart the
    overlay has no stub, so a session launches the agent spec's own entry --
    which would start the server with the literal reference. The overlay alone
    is not enough either: routing turned off still leaves a stale stub there
    until the next start.

    Empty is the fail-closed answer for :func:`_unrouted_secret_reference`: a
    config or overlay that cannot be read routes nothing, so a server with a
    vault reference is withheld rather than launched with the reference
    unresolved.
    """
    try:
        # Deferred: the config loader is heavy and only a server that carries a
        # ``secret://`` reference needs it, so most rebuilds never import it here.
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.mcp_gateway import is_gateway_supported
        from kiro_crew.mcp_gateway.rewriter import resolve_overlay_dir
        from kiro_crew.mcp_gateway.session_servers import injection_server_names

        if not is_gateway_supported():
            # No gateway runs here, so nothing listed is ever served by one.
            return frozenset()
        gateway = KiroCrewConfig.load().mcp_gateway
        saved = frozenset(gateway.stub_servers)
        if not saved:
            return frozenset()
        # ``work_dir=None``: the rebuild writes the USER-level agent spec, so the
        # user-level overlay is the one a session running it is handed stubs from.
        served = injection_server_names(
            resolve_overlay_dir(gateway.overlay_dir), Path(AGENT_FILENAME).stem, work_dir=None
        )
        return frozenset(n for n in served if routed_for_secret_reference(n, saved))
    except Exception:  # noqa: BLE001 -- any failure means "cannot prove routed"
        agent_mod.logger.warning(
            "Could not read the MCP gateway routing; treating every server as "
            "unrouted for secret:// references",
            exc_info=True,
        )
        return frozenset()


def gateway_routed_names() -> frozenset[str]:
    """The servers the running MCP gateway starts; empty on any failure.

    The public read of :func:`_gateway_routed_names` for callers outside the
    materialization (the dashboard's MCP probe and list), so the MCP tab and the
    agent-spec rebuild give the same routing answer. Blocking.
    """
    return _gateway_routed_names()


def _unrouted_secret_reference(
    name: str,
    chosen: dict,
    routed: Callable[[], frozenset[str]],
    app_owned: Callable[[], dict[str, bool]],
) -> bool:
    """True when *name* must be withheld: it names a vault secret nothing will resolve.

    A ``secret://NAME`` env value is resolved from the vault only when the MCP
    gateway starts the server (``mcp_gateway/secret_uri.py``). A server that is
    not routed through the gateway is started by the session from the spec this
    rebuild writes, and that path cannot read the vault -- so the server came up
    with the literal reference as its credential. Withholding it is the documented
    behaviour for a reference that cannot be resolved: the server does not start.

    An app-registered server is exempt. Its env is the app's declaration, and
    whether an installed app may name a vault secret at all is a separate,
    open question this rebuild does not answer; the app registration path warns
    about such a value on its own.

    Names only env KEYS in the warning, never a value or a secret name.
    """
    keys = secret_reference_keys(chosen.get("env"))
    if not keys:
        return False
    # The same helper the overlay rewriter asks before it wraps the server, so
    # the two cannot disagree about which servers the gateway starts.
    if routed_for_secret_reference(name, routed()):
        return False
    if mcp_server_alias(name) in app_owned():
        return False
    from kiro_crew.mcp_gateway import is_gateway_supported  # deferred, like the routing read

    if is_gateway_supported():
        remedy = (
            "Turn on routing for the server in MCP Management, approve its launch "
            "there and restart, so the gateway starts it and resolves the reference "
            "from the vault."
        )
    else:
        # Routing cannot help here: the gateway does not run on this platform.
        remedy = (
            "The MCP gateway does not run on this platform, so no reference can be "
            "resolved here: put the credential in the env directly, or remove the "
            "server."
        )
    agent_mod.logger.warning(
        "Not starting MCP server %r: env %s holds a secret:// reference, which is "
        "resolved only when the MCP gateway starts the server. %s",
        name,
        ", ".join(repr(k) for k in keys),
        remedy,
    )
    return True


def entry_as_enabled(name: str, entry: dict) -> dict:
    """*entry* as the dashboard's enable switch may write it into the agent spec.

    The switch lifts ``disabled``. For a server this rebuild withholds -- a
    stdio command whose env holds a ``secret://`` reference, not started by the
    running gateway -- lifting it hands kiro-cli a live entry with the literal
    reference, and the switch runs no rebuild that would put the mute back. So
    the rebuild's own decision is taken here too, on the same predicate: such an
    entry comes back muted with :data:`WITHHELD_KEY`, which the first pass after
    the server is routed lifts as usual. Any other entry comes back without
    ``disabled`` and without a stale record.
    """
    live = {k: v for k, v in entry.items() if k not in ("disabled", WITHHELD_KEY)}
    if "command" in live and _unrouted_secret_reference(
        name, live, _gateway_routed_names, _app_owned_mcp_keys
    ):
        return _mute_withheld(live)
    return live


def withheld_servers_now_routed() -> bool:
    """True when the agent spec withholds a server the running gateway now starts.

    The boot rebuild runs BEFORE the gateway writes its overlay, so the first
    start after a server is routed still reads the previous start's overlay and
    keeps the server withheld, with its ``tools`` refs stripped. The session is
    handed the gateway's stub all the same (a stub outranks the spec's entry),
    but without the refs it exposes none of the server's tools. The gateway asks
    this once its overlay is written; True means one more rebuild is due. Any
    read failure answers False: the mute stays, which is the safe side.
    """
    try:
        spec = json.loads(
            (agent_mod.kiro_agents_dir_path() / AGENT_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    if not isinstance(servers, dict):
        return False
    withheld = [n for n, e in servers.items() if isinstance(n, str) and is_withheld_mute(e)]
    if not withheld:
        return False
    routed = _gateway_routed_names()
    return any(routed_for_secret_reference(n, routed) for n in withheld)


def _expand_env_values(env: dict, *, server: str | None = None) -> dict:
    """A copy of *env* with home/login names expanded in its values.

    The same rule ``expand_spec_args`` applies to ``args``; any other ``${VAR}``
    stays literal for whichever reader expands it. *env* is not mutated: it is
    often a source config's own dict.
    """
    keys = list(env)
    return dict(zip(keys, expand_spec_args([env[k] for k in keys], server=server)))


def resolve_mcp_servers(
    config: dict,
    sources: McpSources,
    resolve_command: Callable[[str, dict | None], tuple[str | None, str]],
) -> ResolvedServers:
    """Resolve every merged server's command and emit the validated ``mcpServers`` map.

    *resolve_command* is the rebuild's own resolver, handed in so the probe, the
    resolution and the rewriter search one path.
    """
    kirocrew_mcp = sources.kirocrew

    def _resolve_command(cmd: str, env: dict | None) -> tuple[str | None, str]:
        # Home/login names in ``command`` and ``env`` (``env.PATH`` included) are
        # expanded at the resolver boundary, the way the probe and the emitted
        # spec expand them, so every resolution decision below (candidate and
        # provenance precheck alike) searches what the server will be given.
        if isinstance(cmd, str):
            cmd = expand_spec_args([cmd])[0]
        return resolve_command(cmd, _expand_env_values(env) if isinstance(env, dict) else env)

    valid_servers: dict[str, Any] = {}
    # Servers this pass declines to emit ONLY because a declared command did not
    # resolve here. The ref reconcile at the end exempts exactly these: the
    # absence is one a later pass reverses, and an existing config never re-adds
    # a template ref. Every other way a declared server fails to be emitted --
    # no command at all, a spec that is not a dict -- is a real absence whose
    # refs must go, so it is kept out of this set.
    _unresolved_this_pass: set[str] = set()
    _unresolved_commands: dict[str, str] = {}
    # URL servers whose operator OAuth client is bound at write time, by name ->
    # whether the store owns the entry (see `_apply_operator_oauth_client`).
    _oauth_client_targets: dict[str, bool] = {}
    # The store is keyed by its own RAW name, but ``name`` below iterates the
    # config, whose slashed keys a previous pass rewrote to their alias
    # (``_normalize_mcp_server_keys``). A raw-key lookup would read an aliased entry
    # as unmanaged and keep its rendered wire hints, so an edit clearing them would
    # never take effect. Hence alias-keyed, skipping malformed values.
    #
    # A LIST per alias: the mapping is many-to-one, and keeping only the last would
    # leave the other server with no owner.
    _store_by_alias: dict[str, list[dict]] = {}
    for _n, _s in kirocrew_mcp.items():
        if isinstance(_s, dict):
            _store_by_alias.setdefault(mcp_server_alias(_n), []).append(_s)
    _cfg_servers: dict[str, Any] = config.get("mcpServers", {})
    # One spelling of the scope chain, in priority order, for BOTH consumers below:
    # the live-value probe that keeps a rebuild-authored field re-derivable, and the
    # resolution candidate list. The probe's correctness is "this is the value the
    # chain would have resolved", so two separate spellings could drift apart.
    _scopes: tuple[tuple[str, dict], ...] = sources.scopes
    # Read at most once per pass, and only when some server carries a reference.
    _routed = functools.cache(_gateway_routed_names)
    _app_owned = functools.cache(_app_owned_mcp_keys)
    for name, spec in _cfg_servers.items():
        if not isinstance(spec, dict):
            continue
        # This file is BOTH this function's output and one of its inputs: the entry
        # is read back so a field the user set and we never model survives. The
        # resolved absolute ``command`` is ours, not the user's, and read back as
        # authored it could never be rebound: ``_resolve_command`` takes an absolute
        # path without searching. The provenance record re-derives it.
        #
        # The record applies ONLY to a server no other source declares, whose sole
        # home is this file. For a scope-owned server the merge decides the command.
        #
        # Ownership compares by alias, not raw key: ``name`` is the config's, whose
        # slashed spellings an earlier pass rewrote to aliases (see
        # ``_store_by_alias``). Over-matching only declines to apply the record,
        # which is safe; under-matching would let the record shadow a live
        # declaration.
        #
        # A scope owns the COMMAND only when it supplies one: a same-named URL-only
        # or empty entry declares nothing about ``command``, and counting it would
        # strip the record off an agent-only stdio server and strand its stale path.
        # A non-dict value supplies nothing either, as in the candidate chain below.
        #
        # This is a yes/no ownership question, never a choice BETWEEN two declared
        # values.
        _alias_here = mcp_server_alias(name)
        _scope_owned = any(
            any(
                mcp_server_alias(k) == _alias_here
                and isinstance(v, dict)
                and isinstance(v.get("command"), str)
                and v["command"]
                for k, v in _scope.items()
            )
            for _label, _scope in _scopes
        )
        # Captured BEFORE the view rewrites anything: what the entry carried on the
        # way in, and the record's own pair. Together they decide what may be
        # recorded on the way out -- see the emit site below.
        _owned_in = command_is_ours(spec) if not _scope_owned else False
        _pre_cmd = spec.get("command")
        _pair = recorded_source(spec)
        # PRESERVED, never stripped. A scope-owned server's record is not acted on,
        # but if the scope entry later stops resolving or goes away, the server is
        # agent-only again and the record is its only re-derivation source. Keeping
        # it inert is safe: the ownership guard re-checks the emitted value before
        # anything is restored from it.
        _keep_record: tuple[str, str] | None = _pair if _scope_owned else None
        if not _scope_owned:
            _viewed = source_view(spec)
            if not _owned_in or _pair is None:
                # Nothing of ours to restore; the view only strips the key.
                spec = _viewed
            elif _resolve_command(_pair[0], _viewed.get("env"))[0]:
                # The source still resolves, so re-derive from it: that is the whole
                # point, and it is what rebinds a moved binary.
                spec = _viewed
            else:
                # It does NOT resolve, and the emitted config is the entry's only
                # copy, so re-deriving would delete the server for good. A
                # stale-but-working command beats a deleted server: keep it.
                #
                # The record is re-recorded VERBATIM below, so re-deriving stays
                # possible once whatever broke the source clears.
                _keep_record = _pair
                spec = {k: v for k, v in spec.items() if k != DERIVED_KEY}
        # Remote Streamable HTTP servers -- preserved as-is except for the OAuth
        # hints, renamed here (the one boundary) from the internal spelling
        # (``scopes`` / ``clientId``) to the fields kiro-cli deserializes.
        #
        # The dashboard store's own entry answers both ownership and source. A
        # usable dict means the store owns this name and states its hints (in
        # either spelling -- the scope-toggle preservation rule copies a global
        # spec in verbatim, so a store entry can legitimately hold wire form).
        # Anything else -- absent, or a malformed value the merge above skipped
        # and which therefore supplied nothing -- means we own nothing here, and
        # the entry's own wire values are the only copy of configuration written
        # in a file we do not control.
        if spec.get("url"):
            # An entry with no store owner is unmanaged: its own wire values are
            # the only copy of configuration written in a file we do not control,
            # so they are preserved verbatim. That includes a server defined only
            # in the agent config itself (``kiro-cli mcp add --agent kirocrew``, a
            # hand-edit) -- the rebuild merges onto that file, so clearing its
            # hints here would destroy the only copy. Narrowing a grant is the
            # editor's job, where the change is explicit and reversible.
            # A malformed store value contributes nothing -- and "nothing"
            # includes no veto over the alias lookup, so it cannot shadow a
            # usable slashed owner that aliases onto this name. It still does not
            # confer ownership: a name with no usable entry anywhere stays
            # unmanaged, because the fallback yields ``None`` too.
            #
            # ``mcp_server_alias`` is many-to-one, so an alias match is NOT an
            # identity match: an unrelated user-owned name can collide with a
            # managed one. A binding that GRANTS -- these hints are credentials
            # and requested access -- therefore also demands transport identity,
            # or one server's grant lands on another's. (The disabled guard below
            # is the opposite direction and stays name-only on purpose: see there.)
            _store_entry = kirocrew_mcp.get(name)
            if not isinstance(_store_entry, dict):
                _url = spec.get("url")
                _candidates = [
                    c
                    for c in _store_by_alias.get(mcp_server_alias(name), ())
                    if c.get("url") == _url
                ]
                # Nothing in a name says whether normalization minted it or a user
                # typed it, and a url is not an identity when two owners share one.
                # So the collision family is searched only with corroboration that
                # a mint was actually forced -- the plain alias is held by a
                # DIFFERENT transport -- and only when exactly one owner answers.
                # An ambiguous or uncorroborated family leaves the entry unmanaged,
                # because preserving a grant costs less than moving one.
                if not _candidates:
                    _base = mcp_aliases._alias_family_base(name)
                    _held = _cfg_servers.get(_base)
                    if _base != name and isinstance(_held, dict) and _held.get("url") != _url:
                        _candidates = [
                            c for c in _store_by_alias.get(_base, ()) if c.get("url") == _url
                        ]
                _store_entry = _candidates[0] if len(_candidates) == 1 else None
            valid_servers[name] = kiro_oauth_wire_entry(spec, store_entry=_store_entry, server=name)
            # The operator's pre-registered client is NOT bound here. It is read
            # from the vault and written into the entry in `_finalize_and_write`,
            # under the same lock as the spec write, so a rotation that lands
            # between this pass and the commit is what the file carries -- a
            # secret snapshotted here could be retired by the time it is written.
            _oauth_client_targets[name] = _store_entry is not None
            continue
        # A mute this rebuild wrote on an earlier pass is lifted before anything
        # reads the entry, so the decision below is taken afresh every pass.
        spec = _lift_withheld_mute(spec)
        # Build candidate specs in priority order: the merged winner first,
        # then the same server from each source as a resolution fallback, and
        # last the unit the entry ran before its source switched programs.
        candidates: list[tuple[str, dict]] = [("winner", spec)]
        for label, src in _scopes:
            alt = src.get(name)
            if isinstance(alt, dict) and alt is not spec:
                candidates.append((label, alt))
        _previous = sources.replaced_units.get(name)
        if _previous is not None:
            candidates.append(("previous", _previous))

        resolved: str | None = None
        chosen: dict = spec
        tried: list[str] = []
        searched: list[str] = []
        had_any_command = False
        for label, cand in candidates:
            cmd = cand.get("command", "")
            if cmd:
                had_any_command = True
            r, cand_search = _resolve_command(cmd, cand.get("env"))
            if cand_search:
                searched.append(cand_search)
            tried.append(f"{label}={cmd or '<none>'}{' -> ok' if r else ''}")
            if r:
                resolved = r
                chosen = cand
                break
        if chosen is _previous:
            agent_mod.logger.warning(
                "MCP server %r: its new command %s did not resolve; keeping %s",
                name,
                spec.get("command", ""),
                resolved,
            )

        # Kept and muted in place rather than dropped, whichever file declares
        # it: the entry this rebuild writes carries the user's spec-only fields
        # (``disabledTools``, ``autoApprove``), and an agent-only declaration has
        # no other copy. The mute lifts on the first pass after the server is
        # routed; see _mute_withheld.
        _mute_here = bool(resolved) and _unrouted_secret_reference(
            name, chosen, _routed, _app_owned
        )
        if resolved:
            # Start from the merged winner so user-set NON-command fields
            # (autoApprove, disabled, ...) are preserved.  When we fall back to
            # a *different* source, adopt that source's command/args/env as a
            # unit (args belong with their command) — drop the winner's stale
            # args/env so we never pair one source's command with another's
            # args.
            merged = dict(spec)
            merged["command"] = resolved
            if chosen is not spec:
                merged.pop("args", None)
                merged.pop("env", None)
                if "args" in chosen:
                    merged["args"] = chosen["args"]
                if "env" in chosen:
                    merged["env"] = chosen["env"]
            # kiro-cli expands ``${VAR}`` in ``env`` but never in ``args``, and
            # the gateway's pooled spawn reads these same args, so a
            # ``${HOME}/...`` launch path is resolved here or not at all.
            if "args" in merged:
                merged["args"] = expand_spec_args(merged["args"], server=name)
            # A declared env.PATH replaces the child's PATH rather than
            # extending it, so emit the full effective one via the shared
            # normalization point (see emit_env / spec_env_path). emit_env
            # returns a fresh dict: ``dict(spec)`` is shallow, so the env dict
            # here is still the source config's own and must not be mutated
            # through.
            spec_env = merged.get("env")
            if isinstance(spec_env, dict):
                # Home/login names only, as for ``args``, so every reader of
                # this spec sees one value whether or not it expands ``env``
                # itself; any other ``${VAR}`` is left for the reader.
                merged["env"] = emit_env(_expand_env_values(spec_env, server=name))
            # Record only for a server with no other source, and only a field this
            # pass may honestly claim: one the record already proved ours on the way
            # in, or one whose emitted value DIFFERS from what the entry carried.
            # A value merely passed through (a hand edit, an already-absolute
            # declaration) stays unrecorded, so the next pass never overwrites it.
            #
            # Read from the candidate that WON: on a fallback the command came from
            # ``chosen``, so recording ``spec``'s would name a source this entry was
            # not derived from. ``None`` records "the source carried no such field".
            _derived: tuple[str, str] | None = _keep_record
            if _keep_record is not None and resolved != _keep_record[1]:
                # A kept record whose own ``emitted`` value heals to the command
                # written (a saved PATHEXT ``.EXE``) follows it, scope-owned or
                # not: the source stays verbatim, and ``emitted`` names the value
                # on disk, so the next pass does not read it as a hand edit.
                if agent_mod.saved_command_casing(_keep_record[1]) == resolved:
                    _derived = (_keep_record[0], resolved)
            if _keep_record is None and not _scope_owned and (_owned_in or resolved != _pre_cmd):
                _cmd_source = chosen.get("command")
                # Non-empty by construction, but checked: a record with a blank
                # source is unreadable on the way back.
                if isinstance(_cmd_source, str) and _cmd_source:
                    _derived = (_cmd_source, resolved)
            valid_servers[name] = record_derived(merged, _derived)
            if _mute_here:
                valid_servers[name] = _mute_withheld(valid_servers[name])
        elif not had_any_command and mcp_entry_is_registry_governed(spec):
            # A registry pointer (``{"type": "registry"}`` and no command anywhere)
            # is not a broken local entry: kiro-cli wrote it and resolves it by map
            # key against the admin's catalog, which only kiro-cli can read. The
            # spec pins ``includeMcpJson: false``, so this copy is the only way the
            # pointer reaches the session; dropping it would remove every governed
            # server with nothing to explain it. Carried verbatim -- there
            # is no local command to resolve or vouch for, and the catalog decides
            # what launches. Its refs then go through the same shared-server sync
            # and auto-approve gate as any other server. The Claude/KAS projections
            # and the gateway still withhold marked entries on their own.
            valid_servers[name] = dict(spec)
        elif not had_any_command:
            # No candidate defined a command at all — distinct from a command
            # that was defined but couldn't be resolved.
            #
            # Deliberately NOT added to _unresolved_this_pass: there is nothing
            # here for a later pass to resolve, so the ref reconcile below treats
            # this as a real absence and drops the refs. Keeping them would leave
            # an allowedTools grant sitting on a name that any later server can
            # be bound to, and that list never reaches the PreToolUse gate.
            agent_mod.logger.warning("Dropping MCP server %r: no command", name)
        else:
            # A command WAS declared and did not resolve here, which a later pass
            # can reverse once the binary is installed -- so the ref reconcile
            # below keeps this server's refs (see prune_dangling_tool_refs for why
            # a per-tool grant cannot be re-derived).
            _unresolved_this_pass.add(name)
            _unresolved_commands[name] = str(spec.get("command", ""))
            # The searched directories go in the WARNING so a default-level reader
            # can tell "installed outside this path" from "not installed". Built
            # from the paths ACTUALLY searched (a candidate's own env.PATH differs),
            # deduped. The candidate list stays at DEBUG.
            if searched:
                agent_mod.logger.warning(
                    "Dropping MCP server %r: command not found: %s — %s; %s",
                    name,
                    spec.get("command", ""),
                    describe_search_path(dedup_path(os.pathsep.join(searched))),
                    MCP_PATH_HINT,
                )
            else:
                # No candidate was PATH-searched (every command carries a
                # directory), so omit the clause rather than blame an empty PATH.
                agent_mod.logger.warning(
                    "Dropping MCP server %r: command not found: %s; %s",
                    name,
                    spec.get("command", ""),
                    MCP_PATH_HINT,
                )
            agent_mod.logger.debug("MCP %r resolution failed; tried %s", name, "; ".join(tried))
    config["mcpServers"] = valid_servers
    return ResolvedServers(_unresolved_this_pass, _oauth_client_targets, _unresolved_commands)


def sync_shared_server_refs(
    config: dict,
    sources: McpSources,
    mounted: dict[str, str],
    *,
    mount_only: frozenset[str] = frozenset(),
) -> None:
    """Mount every user-installed server, strip disabled ones, and govern their grants.

    *mount_only* names servers recorded as mounted without approval (the default
    agent's mid-chat additions). They take the same path as a governance-ceiling
    withhold: the ``@server`` ref stays in ``tools`` so the server runs, and no
    ``allowedTools`` grant is written, so every call asks.
    """
    kirocrew_mcp = sources.kirocrew
    shared_mcp = sources.kiro_global
    extra_shared_mcp = sources.provider_global
    managed_names = sources.managed_names
    _scopes = sources.scopes
    _mounted_alias_by_source = mounted
    valid_servers: dict[str, Any] = config["mcpServers"]
    # The dashboard store (``kirocrew_mcp``) is in this chain too: it holds every
    # entry the user added through the dashboard, including Connections providers.
    # Omitting it fails silently and totally, because ``tools`` is a CLOSED
    # allowlist (no wildcard): kiro-cli mounts a connected provider and exposes
    # none of its tools, so a fully consented Notion connection answers "I don't
    # have a Notion integration". The entry reaches ``mcpServers`` (via the merges in
    # merge_mcp_sources and resolve_mcp_servers) but never ``tools``.
    _shared_added: list[str] = []
    _shared_removed: list[str] = []
    _shared_not_auto: list[str] = []
    _shared_mount_only: list[str] = []
    # ``disabled`` is TIGHTEST-WINS across scopes, because the scopes disagree by
    # design: ``POST /api/mcp/toggle enabled:false`` writes ``disabled: true``
    # into the kiro global ONLY, so a same-named dashboard-store entry legitimately
    # carries no such key -- and this chain visits the store LAST. Judging each
    # spec in isolation would let that final entry undo the earlier removal,
    # clear the flag off the emitted spec, and re-add the ref to BOTH lists.
    # ``allowedTools`` is the one path that never reaches the PreToolUse gate, so
    # the operator's disable would be silently void for every tool on that server.
    #
    # Mount stripping follows the concrete alias mcp_aliases.normalize_server_keys
    # allocated (``mounted``), so a distinct collision sibling remains mounted.
    # Grant revocation is intentionally looser:
    # every disabled source denies auto-approval to its canonical alias family,
    # because ``allowedTools`` bypasses the PreToolUse gate.
    #
    # "Disabled" is ``mcp_entry_is_muted``, the launch predicate the gateway
    # rewriter, the session projections and the dashboard listing share: a
    # non-boolean ``disabled`` (``"false"``, ``null``) is read FAIL-CLOSED here
    # too, so a server the listing shows as Disabled is never mounted by this
    # rebuild -- truthiness would have mounted one muted with ``null`` or ``0``.
    _shared_source_entries = tuple(
        itertools.chain(extra_shared_mcp.items(), shared_mcp.items(), kirocrew_mcp.items())
    )
    # The rebuild strips a mount on a non-boolean ``disabled`` exactly as the
    # listing withholds the row, so it reports the value the same way -- through
    # the shared bounded warn-once ledger -- rather than silently. A headless
    # install rebuilds without a dashboard read, and would otherwise never say
    # why a server the operator meant to switch on is not mounted.
    for _scope_label, _scope_map in _scopes:
        for _srv, _srv_spec in _scope_map.items():
            _invalid, _flag = invalid_disabled_flag(_srv_spec)
            if _invalid:
                warn_invalid_disabled(_srv, _flag, _scope_label)
    _disabled_source_names = {
        srv for srv, srv_spec in _shared_source_entries if mcp_entry_is_muted(srv_spec)
    }
    _disabled_mounted_aliases = {
        mounted
        for srv, _srv_spec in _shared_source_entries
        for mounted in (_mounted_alias_by_source.get(srv),)
        if mounted is not None and srv in _disabled_source_names
    }
    _disabled_grant_families = {
        mcp_server_alias(srv)
        for srv, srv_spec in _shared_source_entries
        if mcp_entry_is_muted(srv_spec)
    }

    def _grant_ref_is_in_alias_family(ref: object, base: str) -> bool:
        """True when an MCP grant targets ``base`` or a numeric-suffixed sibling."""
        if not isinstance(ref, str) or not ref.startswith("@"):
            return False
        alias = ref[1:].partition("/")[0]
        return mcp_aliases._is_alias_family(alias, base)

    for family in _disabled_grant_families:
        family_ref = f"@{family}"
        allowed = config.get("allowedTools")
        if isinstance(allowed, list):
            kept_allowed = [
                tool for tool in allowed if not _grant_ref_is_in_alias_family(tool, family)
            ]
            if len(kept_allowed) != len(allowed):
                allowed[:] = kept_allowed
                if family_ref not in _shared_removed:
                    _shared_removed.append(family_ref)

    # A server the probe has failed N consecutive times is COUNTED and surfaced,
    # but not unmounted here. The unmount has no safe lever in this file: the
    # generated agent config is simultaneously the mount decision and the only
    # home for agent-only configuration, so dropping an entry destroys whatever
    # lives only there and stamping ``disabled`` makes ``list_servers`` delete the
    # server's own row. See the follow-up issue linked from
    # docs/system-specs/modules/mcp-probe-quarantine.md.
    for name, spec in _shared_source_entries:
        if not isinstance(spec, dict) or name in managed_names:
            continue
        alias = _mounted_alias_by_source.get(name)
        if alias is None:
            continue
        ref = f"@{alias}"
        # The bare spelling is rebuild-owned and is stripped from both lists.
        # Per-tool spellings are stripped only from ``allowedTools``, where the
        # disable defect lives because that list bypasses the PreToolUse gate. A
        # per-tool ``tools`` ref mounts nothing while the map entry is disabled
        # and resumes on re-enable; deleting it destroys a user's selective
        # mount with no recovery lever. This is the same deny-may-be-loose,
        # never-destroy-a-mount asymmetry used by the final reconcile. The ``/``
        # boundary still protects a prefix-sharing server such as ``@aliasx``.

        def _strip_owned_refs(key: str, *, strip_per_tool: bool) -> bool:
            """Drop the bare ref and, when requested, every owned per-tool ref.

            Rebuilds the list in place rather than ``list.remove``, which drops
            only the first occurrence and lets a duplicated ref survive.
            """
            lst = config.get(key)
            if not isinstance(lst, list):
                return False
            kept = without_mcp_refs(lst, (alias,), per_tool=strip_per_tool)
            if len(kept) == len(lst):
                return False
            lst[:] = kept
            return True

        # Muted when ANY scope's entry for this alias mutes it -- the shared
        # multi-scope predicate, so this arm and the dashboard row answer alike.
        # ``spec`` is the merge's winner; the other sources are read too, because
        # a higher-priority ``false`` must never argue a lower scope's mute away.
        muted_here = mcp_entries_muted(
            itertools.chain(
                (spec,),
                (
                    s
                    for srv, s in _shared_source_entries
                    if _mounted_alias_by_source.get(srv) == alias
                ),
            )
        )
        rendered_entry = valid_servers.get(alias)
        withheld_here = isinstance(rendered_entry, dict) and WITHHELD_KEY in rendered_entry
        # A source scope muting the server makes the mute the user's: the
        # withheld record is dropped, or the rewriter would lift the mute the
        # moment the server is routed and start a server the user switched off.
        source_muted = muted_here or alias in _disabled_mounted_aliases
        if source_muted or withheld_here:
            # The rendered entry says ``true`` whenever the server is muted --
            # over a merged ``false`` from a higher-priority scope as much as over
            # a raw ``null``/``0``/``"false"``. The file kiro-cli parses must
            # agree with the listing: a selective ``@srv/tool`` ref this arm keeps
            # would otherwise launch a server every surface calls muted.
            rendered = valid_servers.get(alias)
            if isinstance(rendered, dict):
                rendered["disabled"] = True
                if source_muted:
                    rendered.pop(WITHHELD_KEY, None)
            # A withheld mute alone strips only the bare refs: ``disabled``
            # already keeps the server from starting, and a per-tool grant the
            # user wrote cannot be re-derived once it is gone, while the mute
            # lifts on its own once the server is routed.
            for key in ("tools", "allowedTools"):
                if (
                    _strip_owned_refs(key, strip_per_tool=key == "allowedTools" and source_muted)
                    and ref not in _shared_removed
                ):
                    _shared_removed.append(ref)
        elif alias in valid_servers:
            valid_servers[alias].pop("disabled", None)
            # `tools` is what MOUNTS the server; `allowedTools` additionally
            # auto-approves it — and auto-approve is the one path that never
            # reaches the PreToolUse gate. So a server the enterprise ceiling has
            # an opinion about is mounted but NOT auto-approved: its calls go
            # through the gate, which applies the per-tool rule with the real
            # arguments. Without this the ceiling was un-enforceable for every
            # user-installed MCP server on the primary agent — the same bypass
            # that was closed for app agents, at the second of the two places
            # that write such a list. One predicate serves both.
            may_auto_approve = auto_approve._may_auto_approve(ref) and not any(
                _grant_ref_is_in_alias_family(ref, base) for base in _disabled_grant_families
            )
            # A mount-only server (the default agent's mid-chat addition, see
            # ``template_mcp_additions``) is mounted and never takes the rebuild's
            # whole-server grant. Only the bare ``@server`` grant is the rebuild's
            # own: a per-tool grant the user wrote is theirs, and the card that
            # added the server lists it as still applying, so it is kept. When
            # the ceiling withholds too, the ceiling's stricter arm below runs.
            # Matched on the server's own name, never its alias family: a
            # separately installed ``time-2`` is a different server, and its
            # page approval is not this record's to withhold.
            if may_auto_approve and (name in mount_only or alias in mount_only):
                if ref not in config.get("tools", []):
                    config.setdefault("tools", []).append(ref)
                    if ref not in _shared_added:
                        _shared_added.append(ref)
                allowed_list = config.get("allowedTools")
                if isinstance(allowed_list, list) and ref in allowed_list:
                    allowed_list[:] = [t for t in allowed_list if t != ref]
                if ref not in _shared_mount_only:
                    _shared_mount_only.append(ref)
                continue
            keys = ("tools", "allowedTools") if may_auto_approve else ("tools",)
            for key in keys:
                if ref not in config.get(key, []):
                    config.setdefault(key, []).append(ref)
                    if ref not in _shared_added:
                        _shared_added.append(ref)
            if "allowedTools" not in keys:
                # A grant written before the ceiling arrived must not survive
                # it -- in either spelling, and not as a duplicate either.
                _strip_owned_refs("allowedTools", strip_per_tool=True)
                if ref not in _shared_not_auto:
                    _shared_not_auto.append(ref)
    if _shared_added:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_tools_added",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_added)} added to tools/allowedTools (shared)",
        )
    if _shared_not_auto:
        # Its own SEL record: "mounted but not auto-approved" is a governance
        # outcome an operator has to be able to see, and it is invisible in the
        # added/removed pair (the ref still shows as added, to `tools`).
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_withheld",
            outcome="ok",
            source="install_agent",
            resources=(
                f"{', '.join(_shared_not_auto)} mounted without auto-approve "
                f"(governance ceiling); calls go through the approval gate"
            ),
        )
    if _shared_mount_only:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_auto_approve_withheld",
            outcome="ok",
            source="install_agent",
            resources=(
                f"{', '.join(_shared_mount_only)} mounted without auto-approve "
                f"(mount-only addition from a change card); calls go through the approval gate"
            ),
        )
    if _shared_removed:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_removed)} removed from tools/allowedTools (disabled)",
        )

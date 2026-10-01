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

import itertools
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import agent as agent_mod
from kiro_crew.agent_materialization import auto_approve, mcp_aliases
from kiro_crew.env import MCP_PATH_HINT, dedup_path, describe_search_path, emit_env
from kiro_crew.mcp_cleanup import (
    invalid_disabled_flag,
    mcp_entries_muted,
    mcp_entry_is_muted,
    warn_invalid_disabled,
)
from kiro_crew.mcp_provenance import (
    DERIVED_KEY,
    command_is_ours,
    record_derived,
    recorded_source,
    source_view,
    without_marker,
)
from kiro_crew.mcp_utils import kiro_oauth_wire_entry, mcp_server_alias
from kiro_crew.platform import safe_context_call


def _extra_mcp_scope_globals() -> list[Path]:
    """Provider-global MCP config files contributed by the edition (CPP seam).

    Mirrors ``mcp_discovery._extra_scope_sources`` and the ``/api/mcp/apply``
    uninstall path: the rebuild-time merge reads each seam scope's
    ``global_json`` so a companion's provider global (e.g. Claude Code's
    ``~/.claude.json`` → ``ccGlobal``) is merged into the agent config ONLY when
    that edition contributes it. The Default returns ``[]`` so OSS merges the
    Kiro global only — keeping rebuild symmetric with discovery + apply/uninstall
    (a server the dashboard can't see is never re-merged/resurrected). Fails
    closed to no extra scopes.
    """
    scopes: list = safe_context_call(
        lambda: list(agent_mod.current_context().mcp_tooling.extra_mcp_scopes()),
        fallback_factory=list,
        log_message="extra_mcp_scopes lookup failed; rebuild using core scopes only",
    )
    return [s.global_json for s in scopes]


def _collect_app_mcp_servers(*, audit: bool = True) -> dict[str, Any]:
    """MCP servers contributed by ENABLED apps, keyed ``{app}:{server}``.

    App MCP servers are registered straight into this agent config rather than
    into the shared ``~/.kiro/settings/mcp.json``, because that file is read by
    everything else sharing ``~/.kiro`` — Kiro IDE and any other kiro-cli agent
    — so an app's private tools would leak into surfaces that never installed
    the app. Kiro Crew sessions only ever read the agent config
    (``includeMcpJson`` is pinned False), so writing here is both sufficient and
    properly scoped.

    That makes the app manifests the authoritative source, which this function
    re-derives on every rebuild. Without it a ``clean=True`` rebuild would drop
    every app's servers: clean ignores the existing config, and the entries no
    longer exist in the global file to be re-mirrored from.

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
    # spec: for a `backend.port:"auto"` app the manifest carries an ILLUSTRATIVE
    # port and the reachable one is only known after the backend starts, at which
    # point reregister_app_mcp_servers writes the resolved URL here. Reading the
    # manifest instead would copy the illustrative (dead) port back over the live
    # one on every rebuild, and kiro-cli dials every server in the config — so the
    # app's tools would fail until the next reregister. The manifest is only the
    # fallback for a stdio/command server (no port to resolve); an HTTP server
    # with no live entry is SKIPPED, mirroring _register_mcp_servers' own refusal
    # to ever write a dead-port URL.
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
                    # An HTTP server's manifest URL is only illustrative when the
                    # GATEWAY launches the backend (backend.entryPoint set): the
                    # port is "auto"-resolved and unknown until the process starts,
                    # so with no live entry we skip rather than write a dead port
                    # (mirroring _register_mcp_servers' refusal to write one).
                    # A SELF-MANAGED HTTP server (no backend.entryPoint — e.g. an
                    # independent companion app on a fixed port) has an
                    # authoritative URL and never gets a live registration, so
                    # preserve the manifest URL instead of dropping the server.
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


class _AppOwnership(NamedTuple):
    """What the app manifests claim, and whether this read saw every claim."""

    #: Base alias -> whether every app claiming it is switched on.
    owned: dict[str, bool]
    #: False when ANY app's claim went unread -- an unreadable manifest, an
    #: unreadable enablement, a record ``list_apps`` skipped. An alias missing from
    #: ``owned`` then carries no information, so it cannot be read either as "no app
    #: owns this name" or as positive removal.
    fully_read: bool


def _app_owned_mcp_keys() -> _AppOwnership:
    """Every ALIAS an INSTALLED app declares mapped to its enablement, and whether that is complete.

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
    mints is assigned against the live server map, so it cannot be reproduced
    from manifests here -- which is why the ref reconcile treats family
    membership as a guess: a suffixed sibling inherits nobody's answer, its
    grant goes unless a source vouches for it, and only its mount survives.

    Covers DISABLED apps deliberately: "an app owns this name and is switched off"
    is the case whose grant must not linger on the name, and a collection that
    stops at enabled apps cannot see it. The manifest is the right reader here
    even though :func:`_collect_app_mcp_servers` prefers the live registered map
    for the SPEC, because this needs only the names and a disabled app has no live
    registration to read.

    ``fully_read`` is False whenever ANY claim went unread: an unreadable manifest,
    an unreadable enablement, or a record ``list_apps`` skipped. None of those is the
    answer "no app owns this name". A previous rebuild wrote that app's server into
    the rendered config and :func:`_load_existing_config` carries the entry forward,
    so the name still holds a grant after its owner stops being readable.
    ``get_app_manifest`` returns None for an absent file, a parse failure and a
    permission error alike, and ``list_apps`` drops an app whose installed record
    does not read without raising, so these are the ORDINARY shapes of the failure
    rather than exotic ones.

    Enablement is read through :func:`app_enabled_state`, not
    :func:`is_app_enabled`. That function exists to keep "unreadable" apart from
    "switched off", and this caller needs them apart for the reason its docstring
    gives: the two are indistinguishable in ``is_app_enabled``'s single False, and
    acting on the wrong one here revokes a grant that nothing re-derives.

    Never raises.
    """
    owned: dict[str, bool] = {}
    fully_read = True
    try:
        # Imported lazily for the reason _collect_app_mcp_servers documents:
        # kiro_crew.apps imports back into agent/security, so a module-level
        # import here would close a cycle.
        from kiro_crew.apps.manager import (
            app_enabled_state,
            get_app_manifest,
            list_apps_with_skips,
        )
    except Exception:  # noqa: BLE001 — apps subsystem unavailable
        return _AppOwnership(owned, False)
    try:
        # ``list_apps`` drops an app whose installed record does not read, and drops
        # it SILENTLY rather than raising, so the returned list on its own cannot
        # separate "no such app" from "that app's claim went missing".
        # ``list_apps_with_skips`` answers the second case, and it lives in
        # ``apps.manager`` because the rules it applies -- the installed-record
        # filename, which root entries the listing skips, how presence is judged
        # without resolving a path -- all belong to that module. Reconstructing them
        # here would go stale silently the first time the listing changed.
        apps, _claims_complete = list_apps_with_skips()
    except Exception:  # noqa: BLE001 — an unreadable registry claims nothing KNOWABLE
        return _AppOwnership(owned, False)
    if not _claims_complete:
        fully_read = False
    for app in apps:
        name = app.get("name") if isinstance(app, dict) else None
        if not name:
            # The keys are minted FROM the name, so a nameless row owns no
            # addressable key and its silence costs nothing. Deliberately not an
            # unreadable claim: flagging it would let one malformed row narrow
            # every rebuild's exemption, for no name it could ever have held.
            continue
        try:
            _state = app_enabled_state(name)
        except Exception:  # noqa: BLE001 — an unreadable switch is not a switch-off
            _state = None
        if _state is None:
            # None is "could not read", NOT "disabled": the two are exactly what
            # app_enabled_state was written to keep apart. Its claim is unread, so
            # it names no owner here rather than naming a switched-off one.
            fully_read = False
            continue
        enabled = _state
        try:
            manifest = get_app_manifest(name)
        except Exception:  # noqa: BLE001 — same answer as the None return below
            manifest = None
        if manifest is None:
            fully_read = False
            continue
        for server_name in manifest.mcpServers or {}:
            _alias = mcp_server_alias(f"{name}:{server_name}")
            # AND the answers on a collision. The alias is many-to-one and
            # _normalize_mcp_server_keys hands the loser a `base-<n>` sibling, so
            # one base can stand for several declared servers, across one app or
            # two. The sibling carries no record of WHICH claimant minted it, so
            # the family is treated as switched on only while every app claiming
            # the base is -- the direction that denies rather than grants.
            owned[_alias] = enabled and owned.get(_alias, True)
    return _AppOwnership(owned, fully_read)


# Keys a scope global AUTHORS that are also TRANSPORT-INDEPENDENT, so one the
# source has since DROPPED is dropped here too. Anything else on a merged entry is
# the user's (``autoApprove``, ``disabledTools``, fields we do not model) and
# survives by being ABSENT here, so one invented later defaults to surviving.
# ``command``/``url`` are absent (a transport needs a scope that declares one;
# ``test_mcp_rebuild_reconsumption`` owns that), so their dependants are too --
# reconciling ``headers`` without its ``url`` would pair this source's credential
# with the entry's old endpoint. mcp.md calls this adopting as a unit.
_SOURCE_OWNED_MCP_KEYS = ("timeout", "disabled")


def _merge_source_owned(mcps: dict, name: str, spec: dict, *, stale: set[str]) -> None:
    """Reconcile a scope global's *spec* onto ``mcps[name]``.

    ``setdefault`` was a no-op for a name the config already held, so a source the
    user had CHANGED -- a bumped ``timeout`` -- never reached the generated spec
    again. Only a name in *stale* is reconciled, and reconciling RETIRES it, so a
    name claimed earlier in this pass by a higher-priority scope keeps winning and
    the declared inter-scope precedence is left untouched.
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

    # App-contributed MCP servers go in FIRST so an app's namespaced entry
    # outranks any same-named leftover in the shared global file (every loop
    # below uses setdefault, so whatever lands here wins). Re-derived from the
    # enabled apps' manifests on every rebuild, which is what lets a clean
    # rebuild keep them — see _collect_app_mcp_servers for why apps don't write
    # the global file at all.
    #
    # ASSIGNMENT, not setdefault, for the app's own key. The manifests are the
    # authoritative source and this re-derives them, so `setdefault` kept
    # whatever the PREVIOUS rebuild wrote: a spec whose `autoApprove` this pass
    # had just stripped (the ceiling now governs that server) lost to the stale
    # grant, the tightening never reached an existing config, and those tools
    # kept skipping the PreToolUse gate.
    # Names a PREVIOUS rebuild left behind -- the only stale projections a changed
    # source reconciles. Seeded here so the app loop can retire what it claims.
    _stale = set(config.get("mcpServers", {}))
    for _app_srv, _app_spec in _collect_app_mcp_servers().items():
        if _app_srv not in managed_names:
            config.setdefault("mcpServers", {})[_app_srv] = _app_spec
            # The manifest just spoke, so a same-named shared-file leftover must
            # not reconcile onto it -- this is how the app entry keeps outranking.
            _stale.discard(_app_srv)
            # EXPOSE it: kiro-cli connects entries declared in `mcpServers`, but
            # an unreferenced server contributes no tools to the agent. `tools`
            # is the unconditional exposure list (the final
            # dedup below removes any duplicate); auto-approve stays governed —
            # the spec's `autoApprove` was already ceiling-filtered in
            # _collect_app_mcp_servers, and the final allowedTools pass covers the
            # @ref if it ever lands there.
            config.setdefault("tools", []).append(f"@{_app_srv}")

    shared_mcp = agent_mod._load_json(agent_mod._KIRO_MCP_JSON).get("mcpServers", {})
    for name, spec in shared_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            # Copy so config never aliases the source dict — a later update()
            # (kirocrew merge) must not mutate shared_mcp, which is reused as a
            # fallback candidate during command validation below. The copy also
            # drops our authorship marker: it records who wrote the entry in a
            # SHARED file and has no meaning in a spec we render ourselves, so
            # keeping it would put a key in front of the runtime that says nothing
            # to it.
            _merge_source_owned(config.setdefault("mcpServers", {}), name, spec, stale=_stale)

    # Merge shared MCP servers from edition-contributed provider globals (CPP
    # seam) — now LOWER priority than Kiro global: an absent name is filled and a
    # name claimed earlier in THIS pass was retired from ``_stale``, so these only
    # fill gaps. In OSS the
    # seam is empty, so NO provider global (e.g. ~/.claude.json) is merged —
    # keeping rebuild symmetric with discovery + apply/uninstall so a server the
    # dashboard can't see is never re-merged into sessions. A companion
    # contributes its Claude Code scope here and manages it end-to-end.
    # ``extra_shared_mcp`` accumulates the raw per-scope entries (first scope
    # wins) for the fallback-candidate lookup and shared-server tools sync below
    # (replaces the old single ``cc_shared_mcp``).
    extra_shared_mcp: dict[str, dict] = {}
    for scope_global in _extra_mcp_scope_globals():
        scope_shared_mcp = agent_mod._load_json(scope_global).get("mcpServers", {})
        for name, spec in scope_shared_mcp.items():
            if not isinstance(spec, dict):
                continue
            extra_shared_mcp.setdefault(name, spec)
            if name not in managed_names:
                # Copy (see note above) so the source dict stays pristine for
                # the fallback-candidate lookup.
                _merge_source_owned(config.setdefault("mcpServers", {}), name, spec, stale=_stale)

    # ~/.kiro/crew/mcp.json overrides kiro mcp.json for the kirocrew agent —
    # kirocrew-specific config wins in a tie.
    # Uses update() to merge into existing specs, preserving user-set fields
    # like autoApprove while letting kirocrew's command/args/env win.
    # Skip managed servers for the same reason as above.
    kirocrew_mcp = agent_mod._load_json(agent_mod._user_dir() / "mcp.json").get("mcpServers", {})
    for name, spec in kirocrew_mcp.items():
        if isinstance(spec, dict) and name not in managed_names:
            mcps = config.setdefault("mcpServers", {})
            if name in mcps and isinstance(mcps[name], dict):
                # mcps[name] is a private copy (globals were copied in above),
                # so update() does not mutate any source dict.
                mcps[name].update(spec)
            else:
                mcps[name] = dict(spec)
    return McpSources(kirocrew_mcp, shared_mcp, extra_shared_mcp, managed_names)


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
    _resolve_command = resolve_command
    valid_servers: dict[str, Any] = {}
    # Servers this pass declines to emit ONLY because a declared command did not
    # resolve here. The ref reconcile at the end exempts exactly these: the
    # absence is one a later pass reverses, and an existing config never re-adds
    # a template ref. Every other way a declared server fails to be emitted --
    # no command at all, a spec that is not a dict -- is a real absence whose
    # refs must go, so it is kept out of this set.
    _unresolved_this_pass: set[str] = set()
    # URL servers whose operator OAuth client is bound at write time, by name ->
    # whether the store owns the entry (see `_apply_operator_oauth_client`).
    _oauth_client_targets: dict[str, bool] = {}
    # The store is keyed by its own RAW name, but ``name`` below iterates the
    # config, whose slashed keys a previous pass rewrote to their alias
    # (``_normalize_mcp_server_keys``). Looking the store up by the raw key alone
    # would miss the owner of an aliased entry and fall through to "unmanaged",
    # preserving the wire hints already rendered -- so an edit that cleared them
    # would answer 200 and never take effect. Alias-keyed for that reason, and the
    # mapping skips a malformed value for the same reason the merge does.
    #
    # A LIST per alias, not one entry: the mapping is many-to-one, so two store
    # names can share an alias. Keeping only the last would strip the other server
    # of its owner entirely, and the identity check below would then read it as
    # unmanaged rather than simply looking at the next candidate.
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
    for name, spec in _cfg_servers.items():
        if not isinstance(spec, dict):
            continue
        # This file is BOTH this function's output and, here, one of its inputs: the
        # entry is read back so a field the user set and we never model survives. One
        # field below is ours, not the user's -- the resolved absolute ``command`` --
        # and reading our own computed value back as if it were authored is what made
        # it permanent: ``_resolve_command`` takes an absolute path without searching,
        # so no later change to how commands resolve could rebind one stored once.
        #
        # The record applies ONLY to a server no other source declares -- the one
        # whose sole persisted home is this file, and which therefore has nothing to
        # lose a conflict to. For a scope-owned server, choosing between the record
        # and the live declaration correctly means selecting a per-field source AFTER
        # resolution (the merge picks a winner by which command resolves, then adopts
        # that winner's args/env as a unit), which is a merge-precedence change rather
        # than a provenance one; it is tracked separately. Excluding that population
        # leaves it behaving exactly as it does today.
        #
        # The test is deliberately CONSERVATIVE, and compares by alias rather than by
        # raw key. A scope keys entries by their own raw name while ``name`` here is
        # the config's, whose slash-containing spellings an earlier pass rewrote to
        # aliases (see the ``_store_by_alias`` note above), so a raw-key probe would
        # miss the owner of an aliased entry and wrongly read it as having no other
        # home. Over-matching only declines to apply the record -- today's behavior,
        # and safe. Under-matching would let the record shadow a live declaration.
        #
        # A scope owns the COMMAND only when it actually supplies one. A dict alone is
        # not enough: a same-named URL-only entry, or an empty one, declares nothing
        # about ``command``, so treating it as a competing source would strip the
        # record off an agent-only stdio server and strand its stale path forever.
        # A non-dict value supplies nothing either, and the candidate chain below
        # skips it for the same reason (``isinstance(alt, dict)``).
        #
        # This stays a yes/no ownership question -- does any other source declare a
        # command? -- and never a choice BETWEEN two declared values. Choosing would
        # need the after-resolution ordering this PR is scoped out of.
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
        # PRESERVED, never stripped. A scope-owned server's record is not acted on --
        # no restoration, see above -- but destroying it would be a decision in its
        # own right, and the wrong one: a scope command that does not resolve, or a
        # scope entry that later goes away, would leave the server agent-only again
        # with its only re-derivation source deleted, so a relocated binary could
        # never rebind. Keeping it inert costs nothing, because the ownership guard
        # re-checks the emitted value before anything is ever restored from it, so a
        # record that has gone stale in the meantime is simply not used.
        #
        # Deciding this by which candidate WINS resolution would be the other way to
        # rule out a non-resolving scope command, and that is the after-resolution
        # per-field source selection this change is deliberately scoped out of; it
        # belongs with the agent-config ownership work. Preserving is the part that
        # needs no ordering at all.
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
                # It does NOT resolve, and for this population the emitted config is
                # the entry's only copy -- so re-deriving would drop the server from
                # the map, the file would be rewritten without it, and the next
                # rebuild would have nothing to read. A stale-but-working command
                # beats a deleted server, so keep what we emitted.
                #
                # The record is re-recorded VERBATIM below rather than refreshed from
                # the value we kept: re-deriving must stay possible once whatever
                # broke the source clears, and recording the emitted path as its own
                # source would retire the record's only useful fact.
                _keep_record = _pair
                spec = {k: v for k, v in spec.items() if k != DERIVED_KEY}
        # Remote Streamable HTTP servers — preserved as-is except for the OAuth
        # hints, which are renamed to the fields kiro-cli actually deserializes.
        # This is the one boundary where the internal spelling (``scopes`` /
        # ``clientId``, what mcp.json and the UI use) becomes the wire spelling,
        # so every source file keeps one shape and only the emitted spec changes.
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
        # Build candidate specs in priority order: the merged winner first,
        # then the same server from each source as a resolution fallback.
        candidates: list[tuple[str, dict]] = [("winner", spec)]
        for label, src in _scopes:
            alt = src.get(name)
            if isinstance(alt, dict) and alt is not spec:
                candidates.append((label, alt))

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
            # A declared env.PATH replaces the child's PATH rather than
            # extending it, so emit the full effective one via the shared
            # normalization point (see emit_env / spec_env_path). emit_env
            # returns a fresh dict: ``dict(spec)`` is shallow, so the env dict
            # here is still the source config's own and must not be mutated
            # through.
            spec_env = merged.get("env")
            if isinstance(spec_env, dict):
                merged["env"] = emit_env(spec_env)
            # Record only for a server with no other source, and only a field this
            # pass may honestly claim: one the record already proved ours on the way
            # in, or one whose emitted value DIFFERS from what the entry carried, so
            # we computed it.
            #
            # The excluded case is a value we merely passed through -- a hand edit,
            # or an already-absolute declaration nothing was derived from. It survives
            # this rebuild either way, but a record written over it would read as
            # proof on the NEXT pass, which is how a claim we never earned turns into
            # a value we overwrite. Unrecorded means it stays the user's.
            #
            # Read from the candidate that WON: on a fallback the command came from
            # ``chosen``, so recording ``spec``'s would name a source this entry was
            # not derived from. ``None`` records "the source carried no such field".
            _derived: tuple[str, str] | None = _keep_record
            if _keep_record is None and not _scope_owned and (_owned_in or resolved != _pre_cmd):
                _cmd_source = chosen.get("command")
                # Non-empty by construction -- ``resolved`` is truthy, and it came
                # from resolving THIS candidate's command -- but assert it in the
                # type rather than in a comment: a record whose source is blank is
                # unreadable on the way back, so writing one would silently disable
                # the fix instead of failing here.
                if isinstance(_cmd_source, str) and _cmd_source:
                    _derived = (_cmd_source, resolved)
            valid_servers[name] = record_derived(merged, _derived)
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
            # The searched directories belong in the WARNING, not only at DEBUG:
            # a default-level reader is exactly who needs to tell "installed
            # somewhere this path does not cover" from "not installed at all".
            # Built from the paths the candidates were ACTUALLY searched against
            # and deduped -- a candidate declaring its own env.PATH is searched
            # against a different path, so recomputing one here would name
            # directories that were never consulted. The candidate list stays at
            # DEBUG: that is about which spec won, not about why none resolved.
            if searched:
                agent_mod.logger.warning(
                    "Dropping MCP server %r: command not found: %s — %s; %s",
                    name,
                    spec.get("command", ""),
                    describe_search_path(dedup_path(os.pathsep.join(searched))),
                    MCP_PATH_HINT,
                )
            else:
                # No candidate was PATH-searched (e.g. every command carries a
                # directory component, which shutil.which looks up directly).
                # ``describe_search_path("")`` would render "searched no
                # directories (empty PATH)" and blame a PATH that was never
                # consulted, so omit the clause instead.
                agent_mod.logger.warning(
                    "Dropping MCP server %r: command not found: %s; %s",
                    name,
                    spec.get("command", ""),
                    MCP_PATH_HINT,
                )
            agent_mod.logger.debug("MCP %r resolution failed; tried %s", name, "; ".join(tried))
    config["mcpServers"] = valid_servers
    return ResolvedServers(_unresolved_this_pass, _oauth_client_targets)


def sync_shared_server_refs(config: dict, sources: McpSources, mounted: dict[str, str]) -> None:
    """Mount every user-installed server, strip disabled ones, and govern their grants."""
    kirocrew_mcp = sources.kirocrew
    shared_mcp = sources.kiro_global
    extra_shared_mcp = sources.provider_global
    managed_names = sources.managed_names
    _scopes = sources.scopes
    _mounted_alias_by_source = mounted
    valid_servers: dict[str, Any] = config["mcpServers"]
    # The dashboard store (``kirocrew_mcp``) is in this chain too: it holds every
    # entry the user added through the dashboard, including Connections providers. It
    # Omitting it fails silently and totally, because ``tools`` is a CLOSED
    # allowlist (no wildcard): kiro-cli mounts a connected provider and exposes
    # none of its tools, so a fully consented Notion connection answers "I don't
    # have a Notion integration". The entry reaches ``mcpServers`` (via the merges in
    # merge_mcp_sources and resolve_mcp_servers) but never ``tools``.
    _shared_added: list[str] = []
    _shared_removed: list[str] = []
    _shared_not_auto: list[str] = []
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
        _owned = f"{ref}/"

        def _strip_owned_refs(key: str, *, strip_per_tool: bool) -> bool:
            """Drop the bare ref and, when requested, every owned per-tool ref.

            Rebuilds the list in place rather than ``list.remove``, which drops
            only the first occurrence and lets a duplicated ref survive.
            """
            lst = config.get(key)
            if not isinstance(lst, list):
                return False
            kept = [
                t
                for t in lst
                if t != ref and not (strip_per_tool and isinstance(t, str) and t.startswith(_owned))
            ]
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
        if muted_here or alias in _disabled_mounted_aliases:
            # The rendered entry says ``true`` whenever the server is muted --
            # over a merged ``false`` from a higher-priority scope as much as over
            # a raw ``null``/``0``/``"false"``. The file kiro-cli parses must
            # agree with the listing: a selective ``@srv/tool`` ref this arm keeps
            # would otherwise launch a server every surface calls muted.
            rendered = valid_servers.get(alias)
            if isinstance(rendered, dict):
                rendered["disabled"] = True
            for key in ("tools", "allowedTools"):
                if (
                    _strip_owned_refs(key, strip_per_tool=key == "allowedTools")
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
    if _shared_removed:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_tools_removed",
            outcome="ok",
            source="install_agent",
            resources=f"{', '.join(_shared_removed)} removed from tools/allowedTools (disabled)",
        )

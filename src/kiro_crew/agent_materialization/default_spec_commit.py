"""The rebuild's write of the default agent spec.

Everything that must happen against the FINAL server map, inside the critical section
that ends in the write, happens here: the app servers re-read under
``apps.bridges._mcp_lock`` (the file's other writer), the operator's OAuth client read
from the vault at the last moment (:func:`_apply_operator_oauth_client`), the
dangling-reference reconcile, the KAS permissions seed, the last ``autoApprove``
governance pass, and the Connections alias ownership transaction, opened before the
atomic write and committed after it.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import agent as agent_mod
from kiro_crew.agent_materialization import auto_approve, mcp_aliases, mcp_sources
from kiro_crew.config import config_dir
from kiro_crew.mcp_cleanup import prune_dangling_tool_refs
from kiro_crew.mcp_utils import mcp_server_alias


def _apply_operator_oauth_client(name: str, entry: dict, *, managed: bool) -> dict:
    """Bind the operator's pre-registered OAuth client to a Connections server.

    ``managed`` is whether the dashboard store owns this name (the caller's
    ``_store_entry`` resolved to a usable dict). An UNMANAGED entry is returned
    untouched, apply and strip alike: a server the user hand-authored at a
    provider's URL -- ``kiro-cli mcp add --agent kirocrew`` with their own
    ``oauth.clientId``/``clientSecret`` -- holds the only copy of that client in
    a file the rebuild merges onto, so stripping it here would destroy it and
    overwriting it would swap the user's app for the operator's. The same
    verbatim-preservation rule every other unmanaged wire value gets applies.

    For a managed server, a no-op unless it is a registry provider in
    ``auth.mode = "preregistered"`` at the registry's own URL; for such a
    provider the operator has not configured yet the entry is emitted with the
    three owned keys removed (a cleared client must not survive the merge onto
    the previous spec) and kiro-cli's own DCR attempt fails against the vendor
    exactly as it would today, while the card already says why. When configured,
    the client id, the secret (confidential clients) and the pinned redirect URI
    are written into kiro-cli's ``oauth`` block; see
    ``kiro_crew.connections.oauth_clients`` for the custody argument, in short:
    the vault is the source of truth and this file is a projection of it, the
    same footing ``headers`` secrets already have here.

    Function-local imports: the connections package is otherwise off the agent
    module's import path, and the vault is only opened when a provider actually
    matches, so a plain rebuild with no pre-registered server never decrypts.
    """

    if not managed:
        return entry

    from kiro_crew.connections import get_provider, is_preregistered
    from kiro_crew.connections.oauth_clients import (
        apply_preregistered_oauth_client,
        provider_for_server,
        resolve_oauth_client,
        strip_preregistered_oauth_client,
    )

    provider = provider_for_server(name, entry)
    if provider is None:
        # A managed entry NAMED for a pre-registered provider whose URL does not
        # matches the registry: the operator's client was projected into the
        # previous render for the registry endpoint, and the rebuild merges onto
        # that render, so without this the old secret would ride along to the
        # replacement endpoint. The client is bound to the endpoint it was
        # registered for, so a moved URL retires it from this entry. Any other
        # managed name is not this module's business.
        named = get_provider(name)
        if named is not None and is_preregistered(named):
            return strip_preregistered_oauth_client(entry)
        return entry
    from kiro_crew.secrets import SecretVault

    resolved = resolve_oauth_client(
        provider,
        config=agent_mod._load_json(agent_mod._mc_config_path()) or {},
        vault=SecretVault(config_dir()),
    )
    if resolved is None:
        # The rebuild merges onto the PREVIOUS installed spec, so a client the
        # operator cleared would otherwise survive there verbatim -- a retired
        # secret still presented at the token endpoint and still readable in
        # the file. For a pre-registered provider the operator's record is the
        # only source of these three keys, so absence means removal.
        return strip_preregistered_oauth_client(entry)
    return apply_preregistered_oauth_client(entry, resolved)


def write_default_spec(
    path: Path,
    config: dict,
    *,
    clean: bool,
    gated_off: frozenset[str],
    sources: mcp_sources.McpSources,
    resolved: mcp_sources.ResolvedServers,
    app_owned_at_start: dict[str, bool],
) -> None:
    """Write the default spec: the locked app re-merge, the final reconcile, the commit."""
    kirocrew_mcp = sources.kirocrew
    shared_mcp = sources.kiro_global
    extra_shared_mcp = sources.provider_global
    managed_names = sources.managed_names
    _oauth_client_targets = resolved.oauth_targets
    _narrowed_away = resolved.narrowed_away
    _app_owned_at_start = app_owned_at_start
    # kirocrew.json has TWO independent-locked writers: this regenerating one and
    # the app-MCP registration path (bridges._register_mcp_servers), which does a
    # read-modify-write of the SAME file under bridges._mcp_lock. The rebuild
    # snapshotted the app servers via registered_app_mcp_servers() in
    # mcp_sources.merge_mcp_sources, so a register
    # that lands BETWEEN that snapshot and this write would be silently dropped by
    # our full-file regeneration — "settings or MCP entries silently overwritten".
    # Hold that same lock across a final re-read+merge of the app-namespaced
    # servers so the two writers serialize and neither loses the other's entries.
    # (Only for kirocrew.json — every other agent file this may write has a single
    # writer.) The re-read uses the UNLOCKED reader because we already hold the lock.
    from kiro_crew.apps.bridges import (
        _mcp_json_path,
        _mcp_lock,
        _read_mcp_json_unlocked,
    )

    def _finalize_and_write() -> None:
        servers_map = config.get("mcpServers")
        if isinstance(servers_map, dict):
            # Bind the operator's pre-registered OAuth client HERE, inside the
            # critical section that ends in the spec write (for kirocrew.json the
            # caller holds `_mcp_lock`), and not in mcp_sources.resolve_mcp_servers. The
            # vault is read at the last moment before the commit, so two rebuilds
            # cannot interleave "resolve old secret -> rotation commits new secret
            # -> stale write": whichever rebuild writes last resolved last, and a
            # rotation's own rebuild always runs after its vault write. A retired
            # secret re-emitted into the spec would be presented at the token
            # endpoint and readable in the file until the next rebuild, which is
            # why the read is placed here rather than merely repeated.
            for _oauth_name, _managed in _oauth_client_targets.items():
                _entry = servers_map.get(_oauth_name)
                if isinstance(_entry, dict):
                    servers_map[_oauth_name] = _apply_operator_oauth_client(
                        _oauth_name, _entry, managed=_managed
                    )
        # Reconcile the ref lists against the FINAL server map, at the one funnel
        # every write goes through. Placed here because the map is only final now:
        # mcp_sources.resolve_mcp_servers narrowed it by omission, and the locked app
        # re-merge just above DELETES app entries whose app is not confirmed
        # enabled -- neither touches `tools`/`allowedTools`. Before
        # _seed_kas_permissions, for the same reason that call documents: it reads
        # `allowedTools` to build the KAS policy, so a policy seeded before this
        # would describe refs this then removes.
        #
        # Both exempt sets are absences this rebuild EXPECTS and a later one
        # reverses, so neither is a leftover: a server whose declared command did
        # not resolve on this pass, and a gated-off shipped server whose entry is
        # withheld while "its tools ref is retained" by the decision the withhold
        # audit in managed_mcp.register_managed_refs records. Dropping either would be unrecoverable on an
        # existing config, which deliberately never re-adds a template ref.
        #
        # A commandless declaration and a server deleted by the locked app
        # reconcile are in NEITHER set: nothing reverses those, so their refs go.
        #
        # An app-scoped name survives in the exempt set only while its app is
        # still enabled. A server can fail to resolve here AND have its app
        # switched off, and then the unresolved exemption would keep a grant for a
        # name whose owner is not coming back to re-add the entry. Ownership comes
        # from the manifests that MINT these keys, never from the presence of a
        # colon: a global mcp.json server keyed `npm:foo` keeps its colon through
        # the copy, and an app called `npm` that declares no `foo` does not own it.
        #
        # A key EITHER read claims needs a positive enablement answer here. An app
        # uninstalled mid-rebuild has left both list_apps and its manifest, so a
        # late read alone cannot tell "its app is gone" from "no app ever owned
        # this", and the second of those is the permissive answer. The snapshot
        # taken before this rebuild's work is what keeps the difference readable.
        _app_owned_now = mcp_sources._app_owned_mcp_keys()
        _ever_app_owned = set(_app_owned_at_start) | set(_app_owned_now)
        # A name neither ownership read claims is not proof that nobody owns it:
        # an app whose manifest or enablement does not read names no owner. The
        # rendered config carries a prior rebuild's app entry forward, so the name
        # still holds a grant, and the "nobody owns this" branch would hand it the
        # permissive answer on the one list that never reaches the PreToolUse gate.
        #
        # Narrowed to what no readable source vouches for. ``gated_off`` comes from
        # the managed table and the three scopes from files this rebuild read, so an
        # app read coming back blind cannot make any of them app-owned. Their
        # exemption must not narrow with it: dropping a gated-off server's ref
        # UNMOUNTS it for good, because an existing config never re-adds a template
        # ref once the gate reopens. What is left is an entry only the carried-over
        # config still declares -- exactly the grant with no owner to re-add it.
        _gated_aliases = {mcp_server_alias(_n) for _n in gated_off}
        _vouched_sources = {
            mcp_server_alias(_n)
            for scope in (extra_shared_mcp, shared_mcp, kirocrew_mcp)
            for _n in scope
        }
        _vouched = _gated_aliases | _vouched_sources
        # Which claimant a collision suffix came from is not recoverable (it is
        # assigned against the live server map, and the slashed key it came
        # from is gone by the next rebuild), so family membership -- ``base-2``
        # against a claimed ``base`` -- is a GUESS. The two lists price a guess
        # differently, which is why this reconcile walks no family: the MOUNT
        # survives every candidate here
        # unconditionally (so the guess has nothing left to decide for it), and
        # the GRANT requires positive evidence a guess can never supply (so
        # attribution by family never lends one). What remains is exactness:
        # a name an app claims EXACTLY answers to its own claimant.

        def _base_still_grants(_b: str) -> bool:
            """Whether a claimed base is POSITIVELY still owned and switched on."""
            return _app_owned_now.get(_b) is True

        # Built TWICE, because the two lists fail in opposite directions and one
        # set cannot serve both. `_exempt_mounts` keeps EVERY name this reconcile
        # reached -- and it only ever reaches the unresolved and gated candidates;
        # a server the locked app re-merge deleted is in neither set and loses
        # both refs above. Dropping a `tools` ref can unmount a server for good
        # -- an existing config never re-adds a template ref, and nothing
        # re-adds a user's own suffixed sibling -- while keeping one costs at
        # most a mount attempt against an empty name (an app-claimed UNRESOLVED
        # name loses nothing either: re-enabling the app re-merges its entry and
        # the kept ref resumes mounting it). `_exempt_grants` requires a
        # positive answer, since keeping a grant on doubt leaves an
        # auto-approval on a name any later server inherits -- and
        # `allowedTools` is the path that never reaches the PreToolUse gate.
        # The same mount-survives-doubt / grant-needs-evidence asymmetry the
        # unresolved and gated handling in this reconcile already runs on.
        _exempt_mounts: set[str] = set()
        _exempt_grants: set[str] = set()
        for _n in _narrowed_away | _gated_aliases:
            _exempt_mounts.add(_n)
            if _n in _ever_app_owned:
                # Exactly claimed: its own claimant decides the grant. A
                # readable non-app SOURCE that still declares the name outranks
                # a switched-off claim, because pruning its refs does not merely
                # narrow them: the shared sync re-adds the BARE `@alias` to
                # `allowedTools` once the command resolves again, so a user's
                # per-tool grant comes back as a WHOLE-SERVER one -- widening
                # the very grant this reconcile exists to keep from widening.
                # The managed GATE does not vouch that way: `gated_off` says a
                # shipped server is withheld right now, not that anything still
                # declares the name, so an app whose alias lands exactly on a
                # gated managed name must still lose its grant -- otherwise
                # reopening the gate hands the managed server an approval nobody
                # granted for it.
                if _n in _vouched_sources or _base_still_grants(_n):
                    _exempt_grants.add(_n)
            elif _n in _vouched:
                # Unclaimed -- including a `base-2` sibling nothing claims
                # exactly, whose family guess never lends it an enabled owner's
                # answer. The GRANT is the side that needs a positive answer,
                # and here the vouching supplies it: a readable source still
                # declares the name, or the grant is a gated-off shipped
                # server's own, where revoking it would strand a shipped
                # default. An unclaimed, unvouched name keeps nothing: the
                # auto-approval would sit on the NAME for whatever binds there
                # next, and `allowedTools` never reaches the PreToolUse gate,
                # while dropping it costs one approval a human can grant again.
                _exempt_grants.add(_n)
        # Snapshot the grant list, because only this side is a permission
        # decision: `tools` mounts, `allowedTools` auto-approves.
        _grants_before = [_r for _r in (config.get("allowedTools") or []) if isinstance(_r, str)]
        prune_dangling_tool_refs(config, declared=_exempt_mounts, declared_grants=_exempt_grants)
        _grants_after = set(config.get("allowedTools") or [])
        _revoked = [_r for _r in _grants_before if _r not in _grants_after]
        if _revoked:
            # Dropping a ref out of `allowedTools` REVOKES an auto-approval, which
            # is a permission decision and belongs in the same feed as the
            # auto-approve withholds rather than only in a log line. Auditing must never
            # fail the rebuild.
            try:
                agent_mod.sel().log_api_access(
                    caller="system",
                    operation="mcp_auto_approve_revoked",
                    outcome="ok",
                    source="rebuild_agent_config",
                    resources=(
                        f"{', '.join(_revoked)} auto-approval removed "
                        "(no such server in mcpServers)"
                    ),
                )
            except Exception:  # noqa: BLE001 — auditing never fails the rebuild
                agent_mod.logger.debug(
                    "SEL audit for revoked MCP auto-approvals failed", exc_info=True
                )
        # Runs here, at the single funnel every write path goes through, and AFTER
        # the passes that mutate `allowedTools` (managed/shared MCP sync) — a policy
        # seeded before them would describe a list those passes then replace.
        auto_approve._seed_kas_permissions(config)
        if isinstance(servers_map, dict):
            # LAST governance pass over the assembled server map. `autoApprove` can
            # arrive from an app manifest, a per-agent policy, a managed spec or an
            # imported config; filtering here, on the final map, covers every source.
            config["mcpServers"] = auto_approve._strip_ungoverned_auto_approve(servers_map)
        # THE OWNERSHIP TRANSITION. Everything from here to the commit is one
        # critical section, and it runs for EVERY write that changes the alias map --
        # not only when the alias pass runs. A claim that outlives the generation it
        # describes is the one state that can strip a name the user has since
        # hand-written, and a clean or gate-off rebuild changes the map just as a
        # generated pass does.
        #
        # Imported inside a try: `kiro_crew.connections.alias_record` is a submodule,
        # so importing it executes `kiro_crew.connections.__init__`, which eagerly
        # loads and VALIDATES registry.json at import time (`_PROVIDERS =
        # _load_registry()`). A registry that is corrupt, unreadable or newly invalid
        # therefore raises HERE -- before the fail-closed alias guard below can catch
        # it -- and would abort the whole rebuild, taking the agent spec down over an
        # OPTIONAL feature. The aliases are optional; the spec is not. So on an import
        # failure this still reconciles the on-disk map (a local, import-free helper)
        # and writes the spec, and only the ownership pass is skipped.
        try:
            from kiro_crew.connections.alias_record import (  # noqa: PLC0415
                AliasGeneration,
                begin_transaction,
                commit_transaction,
                load_claimed,
                spec_fingerprint,
            )
        except Exception:  # noqa: BLE001 — an optional feature must not fail the spec
            agent_mod.logger.warning(
                "Skipping Connections tool aliases: the alias ownership module could "
                "not be imported (a broken connections registry does this). The agent "
                "spec is written normally and the aliases already on disk are kept.",
                exc_info=True,
            )
            # Same reconciliation the normal path does, and for the same reason: the
            # assembled map is a PRE-LOCK snapshot, so writing it back would resurrect
            # aliases a concurrent rebuild removed. Clean is exempt (it regenerates
            # from defaults), exactly as at the call below. No ownership transition is
            # opened: with no record module there is no claim to retire, and leaving
            # the record untouched is invariant 4's safe reading -- the pairs on disk
            # are treated as the user's and survive.
            if not clean:
                mcp_aliases._reconcile_tool_aliases_from_disk(path, config)
            agent_mod._atomic_json_write(path, config)
            return

        # `durable` is the generation really on disk, read once inside this section:
        # `config` carries a PRE-LOCK alias snapshot, so an overlapping rebuild would
        # otherwise write its stale copy back, resurrect aliases this one removed,
        # and fingerprint a generation that is gone. It is also the
        # transaction's `previous` candidate, which is what makes a lost spec write
        # recoverable.
        durable_existed, durable_aliases = mcp_aliases._durable_tool_aliases(path)
        previous_fingerprint = spec_fingerprint(durable_aliases if durable_existed else None)
        previous_claim = load_claimed(previous_fingerprint)
        # A CLEAN rebuild regenerates from defaults, so the old spec's map is NOT
        # imported -- importing it would make `toolAliases` the one key that survives
        # the reset the user asked for. It still takes part in the transition above:
        # `durable` is snapshotted as the previous generation so the stale claim is
        # retired rather than left describing a map that is being replaced.
        if not clean:
            mcp_aliases._reconcile_tool_aliases_from_disk(path, config)
        alias_generation = mcp_aliases._apply_connection_tool_aliases(config, previous_claim)
        if alias_generation is None:
            # The pass stood down (gate off, no server map, unreadable registry). The
            # map can still have changed -- a clean rebuild drops it outright -- and
            # then the old claim describes a generation that will not exist, so the
            # transition must still happen with an EMPTY emission to retire it. When
            # the map is unchanged the record is left exactly as it is: rewriting it
            # empty there would forget a real emission and strand those aliases.
            target_fingerprint = spec_fingerprint(config.get("toolAliases"))
            if target_fingerprint == previous_fingerprint:
                agent_mod._atomic_json_write(path, config)
                return
            alias_generation = (target_fingerprint, frozenset())

        target = AliasGeneration(*alias_generation)
        # Opened BEFORE the spec write, so a lost spec write still has a recoverable
        # previous generation (state-table rows 2/3) and a lost commit still resolves
        # to this emission (rows 4/5). Failing to open it FAILS CLOSED on the aliases
        # alone: the map is restored to the durable generation the surviving record
        # still describes, and the rest of the spec is written normally -- an
        # unwritable sidecar must not take down agent-spec repair.
        try:
            begin_transaction(AliasGeneration(previous_fingerprint, previous_claim), target)
        except OSError:
            agent_mod.logger.warning(
                "Skipping Connections tool aliases: the ownership transaction could not "
                "be opened, so the spec's aliases are left at the generation the record "
                "still describes rather than advanced past it.",
                exc_info=True,
            )
            mcp_aliases._set_tool_aliases(config, durable_aliases if durable_existed else None)
            agent_mod._atomic_json_write(path, config)
            return

        agent_mod._atomic_json_write(path, config)
        # The spec carrying those aliases is durable, so the open transaction can be
        # committed -- inside whatever lock guarded that write, so the two land as one
        # unit. Committing outside it would let two rebuilds serialize their spec
        # writes and still commit in the opposite order, leaving a record that
        # describes the OTHER pass's spec. A commit failure PROPAGATES on purpose
        # (alias_record invariant 6): it is recoverable rather than harmful -- the
        # pending record's target fingerprint already matches the map now on disk, so
        # the next pass resolves to exactly this emission (row 5) instead of
        # abandoning it -- but an unwritable data home is still reported when it
        # happens.
        commit_transaction(target)

    try:
        is_kirocrew_json = path.resolve() == _mcp_json_path().resolve()
    except OSError:
        is_kirocrew_json = False
    if is_kirocrew_json:
        with _mcp_lock():
            on_disk = _read_mcp_json_unlocked().get("mcpServers", {})
            if isinstance(on_disk, dict):
                servers = config.setdefault("mcpServers", {})
                # on_disk was written under THIS lock by the app register/deregister
                # path. It is authoritative for a concurrent PORT change (same key,
                # new URL) and for a concurrent REGISTER (a key our snapshot missed),
                # so we overwrite/add from it below. But absence from on_disk is NOT
                # by itself proof that an app server should be dropped: a clean
                # rebuild (or a missing/empty config) starts with an empty on_disk,
                # yet every ENABLED app's manifest-derived servers must still be
                # written — dropping them here made an enabled stdio app's tools
                # vanish. So drop an app server ONLY when its app is confirmed no
                # longer enabled (a concurrent deregister), which is what actually
                # resurrects a dead entry; keep it otherwise.
                try:
                    from kiro_crew.apps.manager import is_app_enabled

                    def _app_of_key_enabled(_key: str) -> bool:
                        try:
                            return bool(is_app_enabled(_key.split(":", 1)[0]))
                        except Exception:  # noqa: BLE001 — cannot verify → fail closed
                            # A malformed installed.json makes enablement
                            # unverifiable. Keeping the entry would leave a
                            # deregistered/unknown app's MCP tools callable with no
                            # way to confirm they should be — so drop it. It is
                            # re-derived from the manifest on the next clean rebuild.
                            return False

                except Exception:  # noqa: BLE001 — apps subsystem unavailable

                    def _app_of_key_enabled(_key: str) -> bool:
                        # If the apps subsystem itself will not import, no app can
                        # be confirmed enabled — drop app-scoped entries rather than
                        # retain unverifiable tools.
                        return False

                on_disk_app = {_k for _k in on_disk if ":" in _k and _k not in managed_names}
                for _k in [k for k in servers if ":" in k and k not in managed_names]:
                    if not _app_of_key_enabled(_k):
                        del servers[_k]
                for _k, _v in on_disk.items():
                    # ALWAYS assign, not add-if-missing: on_disk is authoritative
                    # for app servers, so a concurrent re-registration on a new
                    # port (same key, new URL) must OVERWRITE our stale snapshot —
                    # otherwise the dead pre-rebuild URL is persisted.
                    if _k in on_disk_app:
                        servers[_k] = _v
            _finalize_and_write()
    else:
        _finalize_and_write()

"""MCP server-key aliasing and the Connections tool aliases.

kiro-cli refuses a server key containing ``/``, so :func:`_normalize_mcp_server_keys`
moves such a server under its slash-free alias and carries its ``@`` references with
it, converging re-merged duplicates onto one alias rather than minting a new suffix on
each rebuild, and refusing to let a grant move onto a name a different server holds.

:func:`_apply_connection_tool_aliases` resolves tool-name collisions between exposed
Connections providers into ``toolAliases``. It removes only the pairs the alias record
proves it wrote, so a hand-written alias always survives; the caller owns the
ownership transaction and commits it once the spec is durable.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew import user_json
from kiro_crew.mcp_cleanup import purge_deleted_proxy_from_config
from kiro_crew.mcp_provenance import DERIVED_KEY
from kiro_crew.mcp_utils import mcp_server_alias


def _norm_mcp_spec(spec: Any) -> Any:
    """Return the comparison form of an ``mcpServers`` spec for dedup.

    Setup / re-installs re-emit the same server across runs with slightly
    different optional-key *shapes*: a bare ``{"command": ...}`` one run, then
    ``"env": {}`` or ``"args": []`` the next. Comparing raw dicts treats those
    as distinct servers, so ``_normalize_mcp_server_keys`` mints an ever-growing
    ``-2``/``-3``... suffix on every build / reinstall / update.
    Dropping empty optional collections makes semantically identical re-merges
    collapse onto the canonical alias. An empty ``env``/``args`` is a launch
    no-op for kiro-cli (missing == empty), so this is also the cleaner spec to
    persist.

    :data:`~kiro_crew.mcp_provenance.DERIVED_KEY` is excluded for the same reason,
    and it is load-bearing: the record is our bookkeeping about which field this
    rebuild computed, not part of how the server launches, so an entry carrying one
    and an otherwise-identical re-merged copy without one ARE the same server.
    Comparing it would make them differ and mint the ever-growing suffix this
    function exists to prevent -- and it would do so asymmetrically, since only the
    population with no other config source is ever recorded.
    """
    if not isinstance(spec, dict):
        return spec
    return {
        k: v for k, v in spec.items() if k != DERIVED_KEY and not (k in ("env", "args") and not v)
    }


def _alias_family_base(key: str) -> str:
    """Strip a collision suffix, yielding the alias its family is named for.

    ``_normalize_mcp_server_keys`` preserves a server whose alias is already held
    by a different spec under the lowest free ``<alias>-<n>``, so that key is the
    only name for a server no source spells. Callers resolving ownership through
    it must still confirm identity: sharing a family means sharing an alias, not
    being the same server.
    """
    base, _, tail = key.rpartition("-")
    return base if base and tail.isdigit() else key


def _connection_tool_aliases_enabled() -> bool:
    """True when the Connections tool-alias pass may write ``toolAliases``.

    Read raw from ``config.json`` (the ``kiro_hooks`` precedent) rather than
    declared on the config dataclass: this is a dark-launch gate that retires
    once the alias behaviour is the only behaviour, and an undeclared key costs
    the schema nothing in the meantime.
    """
    connections = (agent_mod._load_json(agent_mod._mc_config_path()) or {}).get("connections")
    if not isinstance(connections, dict):
        return False
    return connections.get("tool_aliases") is True


def _apply_connection_tool_aliases(
    config: dict,
    claimed: frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[str, frozenset[tuple[str, str, str]]] | None:
    """Resolve exposed-provider tool-name collisions into ``config['toolAliases']``.

    Without this, two exposed providers that ship the same tool name leave one of
    the two unreachable -- kiro-cli addresses a tool by bare name, so the later
    mount shadows the earlier one silently.

    :mod:`kiro_crew.connections.tool_aliases` owns invariants 1-6, which decide
    WHICH aliases resolve (registry-sourced, collision-only, exposed-and-verified
    providers). The THREE below are this function's, and govern how a resolution
    is written into a spec that a user also edits:

    * **Flag off => byte-identical emission.** The key is neither created nor
      cleared, so a spec built with the gate off is indistinguishable from one
      built before this pass existed. Nothing else here reads ``toolAliases``,
      so leaving a stale key alone cannot mislead a later pass -- and clearing it
      would make "off" a distinct third behaviour instead of a no-op. A no
      collision resolution likewise writes nothing, so the common install (zero
      or one exposed provider) gains no empty object.

    * **The generated subset is read from a PERSISTED record, not inferred from a
      pair's shape.** Cleanup deletes entries out of a file the user also edits, so
      it needs proof of authorship, and no property of the NAME supplies one: a
      ``<slug>_`` prefix test claims a hand-written ``linear_issues``, and
      re-deriving ``<slug>_<tool>`` claims a hand-written ``notion_search`` for a
      provider that declares nothing. So the pass records exactly what it emitted
      and, on the next run, strips only pairs that record claims (whole triple,
      so a user-edited generated alias does not match and survives). Merging
      onto the previous output instead would make "user-authored wins" preserve
      the LAST rebuild's generated refs: the merge is idempotent, so a rename
      would survive the mount that justified it going away. Every pair the record
      does not claim is by definition the user's and still wins over the registry
      default. See :mod:`kiro_crew.connections.alias_record` for the generation
      binding that stops the record ever describing a spec it does not match: this
      function OPENS the transaction, so an interrupted rebuild is recoverable from
      whichever side actually reached disk, and the CALLER commits it once the spec
      is durable.

    * **A generated alias never lands on a name already in use.** The destination
      is checked against surviving alias targets, the declared natural names of
      exposed providers, every tool name named in a per-tool ``tools`` ref of ANY
      exposed server (custom servers included), and the builtin names in
      ``tools``; a conflict skips that one alias with a warning. Renaming onto an
      occupied name would recreate the shadowing this pass exists to remove, so it
      fails safe to shadowing rather than to a silent overwrite. A custom server
      mounted WHOLE publishes its names only at runtime and is out of scope by
      construction -- see the module docstring's OUT OF SCOPE note.

    Mutates *config* in place. Idempotent: the resolution is a pure function of
    the exposed provider set, so a rebuild that changes no mounts rewrites the
    same map (or leaves the same absence).

    Args:
        config: The assembled spec. Mutated in place.
        claimed: The triples the record proves THIS pass wrote into the generation
            *config* currently carries, already resolved by the caller against the
            authoritative on-disk map. Empty means nothing is provably ours, so
            every existing pair is treated as the user's and survives.

    Returns:
        ``(fingerprint, emitted)`` for the generation this pass just wrote into
        *config* -- the fingerprint of the resulting ``toolAliases`` map and the
        ``(slug, tool, alias)`` triples it emitted, possibly EMPTY (an empty
        emission is how the pass relinquishes pairs it does not write). The
        CALLER owns the transaction: it opens one before the spec write and commits
        it after. ``None`` means the pass did not run -- gate off, no server map, or
        an unreadable registry -- and it has not touched *config*.
    """
    if not _connection_tool_aliases_enabled():
        return None

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return None

    tools = config.get("tools")
    tool_refs = tools if isinstance(tools, list) else []

    try:
        # Imported here, not at module scope: kiro_crew.connections.registry
        # validates the committed registry at MODULE level, so a missing or
        # malformed registry.json would raise on `import kiro_crew.agent` --
        # before the guard below can run -- and take down the very module that
        # installs and repairs the agent spec. Deferring the import keeps a data
        # file from breaking the recovery path, and keeps a registry read off
        # agent.py's import cost for every install that never enables this.
        # Guarded by test_importing_agent_does_not_eagerly_load_the_registry.
        from kiro_crew.connections.alias_record import (  # noqa: PLC0415
            emitted_from_alias_map,
            is_recorded_emission,
            spec_fingerprint,
        )
        from kiro_crew.connections.tool_aliases import (  # noqa: PLC0415
            exposed_declared_tools,
            natural_tool_names,
            resolve_tool_aliases,
            statically_visible_tool_names,
        )

        previously_emitted = claimed
        exposed = exposed_declared_tools(servers, tool_refs)
        aliases = resolve_tool_aliases(exposed)
        reserved_natural = natural_tool_names(exposed)
        reserved_visible = statically_visible_tool_names(tool_refs)
    except Exception:  # noqa: BLE001 — a malformed registry must not fail a rebuild
        agent_mod.logger.warning(
            "Skipping Connections tool aliases: registry unavailable", exc_info=True
        )
        return None

    existing = config.get("toolAliases")
    # A pre-existing non-dict value (hand-edited ``toolAliases: []``) is replaced
    # rather than merged onto: kiro-cli rejects the whole spec over it, so
    # self-healing costs nothing a working config would miss.
    existing_map = existing if isinstance(existing, dict) else None

    # Drop only the pairs the RECORD proves this pass wrote, and keep everything
    # else, whose authorship is unproven and therefore the user's. Then recompute;
    # see the staleness invariant above. The comparison is on the whole triple, so
    # a generated alias the user has since edited does not match and stays. A
    # non-string alias is dropped for the same reason a non-dict container is
    # replaced: kiro-cli rejects the entire spec over it, so preserving it would
    # protect a hand-edit by costing the user every tool.
    retained = {
        ref: alias
        for ref, alias in (existing_map or {}).items()
        if isinstance(ref, str)
        and isinstance(alias, str)
        and not is_recorded_emission(previously_emitted, ref, alias)
    }

    # Destination guard: everything a generated alias must not collide with.
    occupied = set(retained.values())
    occupied |= reserved_natural
    occupied |= reserved_visible
    occupied |= {ref for ref in tool_refs if isinstance(ref, str) and not ref.startswith("@")}

    accepted: dict[str, str] = {}
    for ref, alias in aliases.items():
        if ref in retained:
            # Hand-authored override for this exact ref: the user's alias stands
            # and the generated one is not a second entry.
            continue
        if alias in occupied:
            agent_mod.logger.warning(
                "Skipping Connections tool alias %s -> %r: the name is already in use "
                "by another alias or tool, so renaming onto it would shadow that tool",
                ref,
                alias,
            )
            continue
        accepted[ref] = alias
        occupied.add(alias)

    emitted = emitted_from_alias_map(accepted)
    merged = dict(sorted({**accepted, **retained}.items()))

    # The generation this pass is about to write. Nothing generated AND nothing
    # hand-authored surviving means the key goes away entirely rather than being
    # emptied: absent stays absent (gate-off parity), and a key holding only this
    # pass's now-stale output returns the spec to exactly the shape it had before
    # any alias was ever written.
    target = (spec_fingerprint(merged or None), emitted)

    if not merged:
        if existing is not None:
            config.pop("toolAliases", None)
            agent_mod.logger.debug(
                "Cleared Connections tool aliases: no collisions among exposed providers"
            )
    elif merged != existing:
        config["toolAliases"] = merged
        agent_mod.logger.debug(
            "Connections tool aliases written: %s generated, %s retained (%s)",
            len(accepted),
            len(retained),
            ", ".join(f"{ref}->{alias}" for ref, alias in merged.items()),
        )
    return target


def _is_alias_family(key: str, base: str) -> bool:
    """True if ``key`` is ``base`` or a ``base-<n>`` numeric-suffixed sibling.

    ``mcp_server_alias`` is many-to-one, so :func:`_normalize_mcp_server_keys`
    gives the loser of a collision the lowest free ``base-<n>`` rather than
    dropping a distinct server. One base alias therefore stands for a FAMILY of
    concrete keys. The family rule serves KEY NORMALIZATION only
    (converging equivalent duplicates onto one alias): the ref reconcile
    deliberately consults no family, because which claimant a suffix came from
    is not recoverable there -- its mount exemption is unconditional and its
    grants require an exact claim.
    """
    return key == base or (key.startswith(f"{base}-") and key[len(base) + 1 :].isdigit())


def _normalize_mcp_server_keys(
    config: dict,
    *,
    reserved_keys: Collection[str] = (),
    removed_grants: list[str] | None = None,
) -> dict[str, str]:
    """Rewrite any slash-containing ``mcpServers`` key to its slash-free alias.

    Mutates ``config`` in place: moves each affected server spec under its
    alias key and rewrites (and de-duplicates) the matching ``@oldkey`` ->
    ``@alias`` reference in ``tools``/``allowedTools``. Returns each input key's
    concrete mount alias so later ownership decisions survive collision suffixing.
    Migrates already-broken existing configs. Idempotent: slash-free keys are left
    untouched and a re-merged duplicate collapses onto the canonical alias (no churn).

    ``reserved_keys`` names known-but-absent servers. Their refs are left
    exactly as they are: no present key's per-tool prefix rewrite may capture
    one, and nothing moves one onto an alias this function may hand to a live
    server further down. An absent slashed server's refs therefore reach the
    final dangling-ref reconcile unchanged and are dropped there -- it has no
    ref-survival guarantee, and minting one is what lets an ``allowedTools``
    grant move between servers. When provided, ``removed_grants`` receives only
    grants this pass drops for a reserved-alias collision; rewrites and duplicate
    collapse are not revocations.

    Dedup is by *normalized* spec (:func:`_norm_mcp_spec`), so a re-added key
    that differs only by an empty ``env``/``args`` reuses the existing alias
    instead of accumulating a fresh ``-N`` suffix on every build / reinstall /
    update. Convergence: any already-suffixed sibling that is an
    equivalent duplicate is folded back onto the surviving alias (its ``@ref``
    is redirected), so a config already polluted by the pre-fix bug self-heals.

    Collision: if the alias is held by a *genuinely different* spec, the server
    is preserved under the lowest free numeric-suffixed alias (``-2``, ``-3``)
    -- never dropped. Managed servers (slash-free by construction) are skipped
    so their dynamic-field refresh is never disturbed.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    managed = set(agent_mod._MANAGED_MCP_SERVERS)
    mounted_aliases = {key: key for key in servers}

    def _is_family(key: str, base: str) -> bool:
        """True if ``key`` is ``base`` or a ``base-<n>`` numeric-suffixed sibling."""
        return _is_alias_family(key, base)

    def _rewrite_ref(ref: object, old_ref: str, new_ref: str) -> object:
        # Both spellings kiro-cli resolves move together: the bare ``@server``
        # and the per-tool ``@server/tool``. Leaving the per-tool spelling on
        # the old key strands it on a name this pass removes, and the final
        # reconcile then drops it from BOTH lists as dangling. The suffix is
        # carried VERBATIM by slicing off the matched prefix -- never re-derived
        # by splitting the ref, because the server key itself may contain a
        # slash (``npm:@scope/pkg``), so a split takes the wrong component.
        if isinstance(ref, str) and ref in _immovable:
            return ref
        if ref == old_ref:
            return new_ref
        if isinstance(ref, str) and ref.startswith(f"{old_ref}/"):
            return new_ref + ref[len(old_ref) :]
        return ref

    # A known-but-absent slashed server's refs are IMMOVABLE. They need
    # protecting from exactly one thing -- a present ANCESTOR reading the absent
    # descendant's bare ``@a/b/c`` as its own per-tool spelling -- and moving
    # them to the absent server's own alias buys that protection at the price of
    # MINTING a name this same function hands out further down (the canonical
    # alias, or a ``base-<n>`` from the collision path below), so whatever lands
    # there inherits an ``allowedTools`` grant belonging to the absent server,
    # on the one list that never reaches the PreToolUse gate. No "is the alias
    # free" test can close that: the name is allocated AFTER the test runs.
    # Frozen instead, the refs address a name the final map does not hold and
    # the reconcile drops them -- exactly as it did before this pass learned the
    # per-tool spelling -- and no grant can travel.
    #
    # Ownership is resolved LONGEST-match across both sets, not "some reserved
    # key claims this ref": with a reserved ancestor ``a/b`` and a PRESENT
    # descendant ``a/b/c``, the bare ``@a/b/c`` belongs to the live key, and
    # freezing it would strand that server with no ref instead.
    #
    # EVERY absent reserved key participates -- slash-free ones included. A
    # slash-free unresolved key (``foo-bar``) is its own alias, so a present
    # ``foo/bar`` normalizing onto that exact name is the same
    # grant-inheritance hole as the slashed case: excluding it here kept its
    # stale ``@foo-bar`` grant invisible to the collision filter below, and
    # the grant auto-approved whatever live server landed on the name. The
    # ``key not in servers`` guard still keeps every PRESENT key out, and
    # longest-match ownership keeps a reserved name from claiming a live
    # descendant's refs.
    _reserved = {key for key in reserved_keys if isinstance(key, str) and key not in servers}
    _claimable = _reserved | set(servers)

    def _owner(ref: str) -> str:
        """The longest key claiming ``ref``; an exact bare match always wins."""
        return max(
            (k for k in _claimable if ref == f"@{k}" or ref.startswith(f"@{k}/")),
            key=len,
            default="",
        )

    _immovable: set[str] = set()
    for _key in ("tools", "allowedTools"):
        _lst = config.get(_key)
        if isinstance(_lst, list):
            _immovable |= {
                r
                for r in _lst
                if isinstance(r, str) and r.startswith("@") and _owner(r) in _reserved
            }

    # Phase A allocates every final mount key before any reference moves.
    # Longest-first keeps an exact descendant ahead of its ancestor when their
    # slash-containing names overlap; equal-length keys cannot prefix each
    # other, so their order is immaterial.
    renames: dict[str, str] = {}
    for old_key in sorted(
        [k for k in servers if "/" in k and k not in managed], key=len, reverse=True
    ):
        spec = _norm_mcp_spec(servers.pop(old_key))
        base = mcp_server_alias(old_key)

        # Reuse an existing home for an equivalent spec — the canonical alias or
        # any already-suffixed sibling — instead of minting a new suffix, so
        # repeated re-merges converge rather than accumulate.
        alias = next(
            (k for k in servers if _is_family(k, base) and _norm_mcp_spec(servers[k]) == spec),
            None,
        )
        if alias is None:
            # Genuinely distinct spec (or nothing here yet): take the canonical
            # alias if free, else the lowest free numeric suffix (never drop a
            # distinct server).
            alias = base
            if alias in servers:
                n = 2
                while f"{alias}-{n}" in servers:
                    n += 1
                alias = f"{alias}-{n}"
        servers[alias] = spec
        renames[old_key] = alias
        mounted_aliases[old_key] = alias

        # Converge any OTHER sibling that duplicates the spec we just placed:
        # drop it and redirect its @ref onto the surviving alias.
        for dup in [
            k
            for k in list(servers)
            if k != alias and _is_family(k, base) and _norm_mcp_spec(servers[k]) == spec
        ]:
            del servers[dup]
            for source, target in renames.items():
                if target == dup:
                    renames[source] = alias
            renames[dup] = alias
            for source, target in mounted_aliases.items():
                if target == dup:
                    mounted_aliases[source] = alias

        agent_mod.logger.info("Normalized MCP server key %r -> %r (kiro-safe)", old_key, alias)

    # Phase B resolves ownership from the untouched ref and maps that original
    # prefix straight to its final alias. A moved ref is never matched again.
    def _moved_once(ref: object, *, grant: bool = False) -> object:
        if not isinstance(ref, str):
            return ref
        owner = _owner(ref)
        alias = renames.get(owner)
        if alias is None:
            return ref
        if grant and "/" in owner:
            runtime_server = ref[1:].split("/", 1)[0]
            # allowedTools follows the runtime's first-slash parsing. When both
            # readings name claimable servers, preserving the runtime reading
            # keeps a per-tool grant narrow instead of granting the renamed
            # owner as a whole server.
            if runtime_server != owner and runtime_server in _claimable:
                return ref
        return _rewrite_ref(ref, f"@{owner}", f"@{alias}")

    for key in ("tools", "allowedTools"):
        lst = config.get(key)
        if isinstance(lst, list):
            config[key] = list(
                dict.fromkeys(_moved_once(ref, grant=key == "allowedTools") for ref in lst)
            )

    _reserved_aliases = {mcp_server_alias(key) for key in _reserved}

    def _collides_with_reserved_alias(ref: object) -> bool:
        """True when a live server occupies an absent server's alias family."""
        if not isinstance(ref, str) or not ref.startswith("@"):
            return False
        alias = ref[1:].split("/", 1)[0]
        return alias in servers and any(_is_alias_family(alias, base) for base in _reserved_aliases)

    allowed = config.get("allowedTools")
    if isinstance(allowed, list):
        kept_allowed: list[object] = []
        for ref in allowed:
            dropped = (
                isinstance(ref, str)
                and ref in _immovable
                and ref.startswith("@")
                and ref[1:].split("/", 1)[0] in servers
            ) or _collides_with_reserved_alias(ref)
            if dropped:
                if removed_grants is not None and isinstance(ref, str):
                    removed_grants.append(ref)
            else:
                kept_allowed.append(ref)
        config["allowedTools"] = kept_allowed

    return mounted_aliases


class _UnreadableSpec:
    """What :func:`_durable_tool_aliases` reports for a spec it could not read.

    The file exists, so it is not "no durable generation", and what it holds is
    unknown, so it is not an empty one either. Each caller refuses the write on
    it (:func:`_raise_if_unreadable`) rather than fingerprint or reconcile
    against a guess.
    """

    __slots__ = ("error",)

    def __init__(self, error: OSError) -> None:
        self.error = error


def _raise_if_unreadable(aliases: object) -> None:
    """Raise the rebuild's unreadable-spec error when *aliases* is :class:`_UnreadableSpec`."""
    if isinstance(aliases, _UnreadableSpec):
        error = aliases.error
        raise agent_mod._AgentSpecUnreadable(f"{type(error).__name__}: {error}") from error


def _durable_tool_aliases(path: Path) -> tuple[bool, object]:
    """Read the ``toolAliases`` generation the spec ON DISK carries right now.

    The single reader behind both the pre-write reconcile and the ownership
    transition, so the value the record is fingerprinted against and the value
    written into the spec can never come from two different reads.

    Returns:
        ``(existed, aliases)`` -- *existed* is False when there is no spec file
        (so there is no durable generation at all, which is NOT the same as a spec
        holding an invalid one). A spec that exists but cannot be read is
        ``(True, _UnreadableSpec)``, never "missing", because the read is retried
        through :func:`read_bytes_with_retry` and only ``FileNotFoundError`` means
        absent. Otherwise *aliases* is the raw value the spec carries, which
        :func:`_set_tool_aliases` and
        :func:`~kiro_crew.connections.alias_record.spec_fingerprint` both read as the
        absent generation when it is not a usable map.
    """
    try:
        data = agent_mod.read_bytes_with_retry(path)
    except FileNotFoundError:
        return (False, None)
    except OSError as exc:
        return (True, _UnreadableSpec(exc))
    try:
        # Decoded inside the ``try``: invalid UTF-8 is a ``ValueError`` and reads
        # as an invalid spec, the same as invalid JSON, so a rebuild still runs.
        on_disk = user_json.loads_user_json(data.decode("utf-8"))
    except ValueError:
        return (True, None)
    return (True, on_disk.get("toolAliases") if isinstance(on_disk, dict) else None)


def _set_tool_aliases(config: dict, aliases: object) -> None:
    """Put *aliases* into *config*, removing the key when there is no usable map."""
    if isinstance(aliases, dict):
        config["toolAliases"] = aliases
    else:
        config.pop("toolAliases", None)


def _reconcile_tool_aliases_from_disk(path: Path, config: dict) -> bool:
    """Align ``config['toolAliases']`` with the generation the spec ON DISK carries.

    ``config`` is assembled from a spec read taken BEFORE the write lock, so its
    alias map can be a stale generation by the time the write happens. Two
    overlapping rebuilds serialize their spec writes but not that read: the second
    would otherwise write its pre-lock snapshot back, resurrecting aliases the
    first had removed, and would fingerprint a generation that is gone.

    So the map is re-read here, inside the critical section that writes it, and
    that value is what the alias pass resolves against (alias_record invariant 7).
    It runs whether or not the alias pass will: a gate-off or fail-closed rebuild
    would write the stale snapshot just the same. A CLEAN rebuild is the one
    exemption and the caller makes it -- clean regenerates from defaults, so
    importing the old spec's map would defeat the reset (see the call site).

    A MISSING spec is not an invalid one. With no file there is no durable
    generation to reconcile against, so the assembled map stands: a first install
    whose ``agent.json`` carries a hand-written ``toolAliases`` would otherwise
    have it erased before it was ever written. Only a spec that EXISTS decides the
    map -- a dict is imported, and an absent or non-dict value clears the key,
    because kiro-cli rejects the whole spec over a non-dict ``toolAliases`` and
    re-importing one would carry the broken file forward and cost the user every
    tool. Dropping it here repairs it even when the alias pass never runs.

    Uses the file being written rather than a fixed path, so it is correct on the
    canonical spec (where the caller holds the lock) and on any other spec the
    rebuild targets.

    A spec that exists but cannot be read raises the rebuild's unreadable-spec
    error, so nothing is written over it.

    Returns:
        True when a spec existed on disk and therefore decided the map; False when
        there was none and *config* was left exactly as assembled.
    """
    existed, aliases = _durable_tool_aliases(path)
    _raise_if_unreadable(aliases)
    if existed:
        _set_tool_aliases(config, aliases)
    return existed


def normalize_server_keys(config: dict, unresolved: set[str]) -> dict[str, str]:
    """Alias slash-containing server keys, purge the deleted proxy, and audit revocations.

    Returns each source key's concrete mount alias.
    """
    _unresolved_this_pass = unresolved
    # Rewrite slash-containing server keys to kiro-safe aliases (also migrates
    # already-broken configs); runs after merges so global-only servers and
    # their stale @refs are normalized too. The normalizer reports only grants
    # its terminal collision filter removes; rewrites and dedup stay non-revoking.
    _alias_purge_removed_grants: list[str] = []
    _mounted_alias_by_source = _normalize_mcp_server_keys(
        config,
        reserved_keys=_unresolved_this_pass,
        removed_grants=_alias_purge_removed_grants,
    )
    _proxy_purge_grants_before = list(
        dict.fromkeys(ref for ref in (config.get("allowedTools") or []) if isinstance(ref, str))
    )

    # Drop any server whose argv invokes the deleted mcp-playwright-proxy
    # subcommand.  Runs on EVERY rebuild because the entry can be
    # re-injected from ~/.kiro/crew/mcp.json by the merges above.  The
    # first-run marker-guarded purge (clean_stale_managed_mcp) covers the
    # GLOBAL ~/.kiro/settings/mcp.json, which is a different file and a
    # different ownership boundary; this covers the assembled agent config.
    purge_deleted_proxy_from_config(config)
    _proxy_purge_grants_after = {
        ref for ref in (config.get("allowedTools") or []) if isinstance(ref, str)
    }
    _alias_purge_revoked_by_purge = [
        ref for ref in _proxy_purge_grants_before if ref not in _proxy_purge_grants_after
    ]
    _alias_purge_revoked = list(
        dict.fromkeys((*_alias_purge_removed_grants, *_alias_purge_revoked_by_purge))
    )
    if _alias_purge_revoked:
        try:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_revoked",
                outcome="ok",
                source="rebuild_agent_config",
                resources=(
                    f"{', '.join(_alias_purge_revoked)} auto-approval removed "
                    "(reserved-alias collision or deleted-proxy purge)"
                ),
            )
        except Exception:  # noqa: BLE001 — auditing never fails the rebuild
            agent_mod.logger.debug("SEL audit for revoked MCP auto-approvals failed", exc_info=True)
    return _mounted_alias_by_source

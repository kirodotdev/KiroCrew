"""Policy over the managed MCP servers: which are emitted, and which fields are Crew's.

:data:`kiro_crew.agent._MANAGED_MCP_SERVERS` names the servers and how each launches;
this module decides what a spec writer does with them. One eligibility predicate
(:func:`_mcp_server_emission_eligible`) answers "would a rebuild emit this" for both
spec writers and for the dashboard's merge-on-write host set, and
:func:`_enforce_managed_mcp_ownership` is the one allow-list of the fields a managed
entry may carry, applied identically by the fresh build (:func:`emit_managed_servers`)
and the refresh (:func:`refresh_managed_servers`) so the two cannot drift.

The data-home pin (:func:`_managed_mcp_env`) and the enterprise registry marker are
re-derived on every pass: both describe the gateway that is running, never what an
earlier writer left in the file.
"""

from __future__ import annotations

import math
from typing import Any

from kiro_crew import agent as agent_mod
from kiro_crew.env import sanitize_spec_env
from kiro_crew.platform.governance import CU_MCP_SERVER


def _managed_mcp_env() -> dict[str, str]:
    """Env every managed Kiro Crew MCP server is launched with.

    Pins ``KIROCREW_HOME`` when the gateway is running under an override, because
    a child process does NOT inherit it: the spec's ``env`` is the only channel.
    That pin doubles as the spec's write provenance — see
    :func:`_existing_specs_are_mine`.
    Without this the gateway and its own stdio shims read DIFFERENT data homes,
    which is silent and self-contradictory rather than merely wrong —
    ``computer_use.json`` is written to the override home by Settings while
    ``mcp_computer`` reads the DEFAULT home, so the panel shows the feature ON
    while the shim publishes an empty ``tools/list`` and the agent truthfully
    reports it has no computer-use tools. The same split would desynchronise the
    cron store and the lessons file.

    Resolved through ``_valid_override_home`` rather than reading the env var
    directly, so an override the loader REFUSES (a filesystem root, ``/usr``) is
    not propagated to children that would then disagree with the gateway in the
    other direction.

    Returns ``{}`` on a default install, which keeps the emitted spec
    byte-for-byte what it is today (``_prune_empty`` drops an empty ``env``).
    That is safe only because a default-install child DERIVES the same home the
    gateway did, from an inherited ``HOME``. Preserving a user's ``env`` puts
    that inheritance in reach of a config, so the companion control lives in
    ``_enforce_managed_mcp_ownership``: see ``_HOME_DERIVING_ENV_KEYS``.
    """
    override = agent_mod._valid_override_home()
    return {"KIROCREW_HOME": str(override)} if override else {}


# Declaration discriminator kiro-cli reads for enterprise MCP governance. It is
# NOT a transport: a `registry` entry is a POINTER into the admin's catalog,
# carrying only env/headers/timeout overrides, and its command/url are ignored.
_MCP_REGISTRY_TYPE = "registry"

# Every key a managed MCP server's entry may carry. Derived from kiro-cli's
# documented local-server schema rather than assembled by hand, so it can be
# reviewed against an external source instead of against someone's memory:
# docs/reference/kiro-cli/mcp/configuration.md lists command, args, env,
# disabled, autoApprove and disabledTools for a local (stdio) server, and
# url/headers for a REMOTE one. The remote pair is deliberately absent -- these
# servers are stdio-only, a leftover ``url`` would shadow the command, and older
# builds left both behind -- so they are dropped by the rule below rather than by
# name. ``timeout`` is the one addition: not in that table, but emitted by base
# and one of the customizations this rule preserves, so dropping it would
# re-introduce the very bug this filter exists to prevent.
#
# ``type`` is here, and only the ``registry`` VALUE is ours. A transport hint the
# user wrote (``"type": "stdio"``) is theirs and kiro-cli tolerates it, which
# ``test_refresh_preserves_a_user_transport_hint`` pins deliberately -- so this
# key is carried and the registry marker is re-derived from the signed-in account
# below, rather than the whole key being treated as ours.
#
# Of the rest, three are OURS and are set on each pass (``command``, ``args``,
# ``autoApprove``); the other four are the customizations a user may declare and
# this fix exists to preserve. ``disabledTools`` is a user GUARD, not a
# preference: dropping it would silently re-expose tools the user turned off,
# which is why it is carried rather than re-derived (the custom-server PUT
# endpoint round-trips it for the same reason).
_MANAGED_MCP_ENTRY_KEYS: frozenset[str] = frozenset(
    {"command", "args", "type", "autoApprove", "timeout", "env", "disabled", "disabledTools"}
)

# The type each USER-AUTHORED key must have, from the same schema table as the set
# above. A right key with a wrong-typed value is rejected by kiro-cli exactly like
# an unknown field -- and it rejects the whole agent -- so these are checked and
# dropped in ``_enforce_managed_mcp_ownership`` rather than trusted.
#
# ``command`` and ``args`` are absent because both callers set them before the
# enforcer runs, so their types are ours rather than input. ``env`` is absent
# because it needs more than a type check: ``sanitize_spec_env`` already validates
# it per ENTRY, which is the finer-grained version of this same rule.
_MANAGED_MCP_ENTRY_VALUE_TYPES: dict[str, type | tuple[type, ...]] = {
    "type": str,
    "timeout": (int, float),
    "disabled": bool,
    "autoApprove": list,
    "disabledTools": list,
}

# The ITEM type for each list-valued key above. Both are documented as arrays of
# tool NAMES, so validating only the container leaves ``disabledTools: [1]``
# emitting a spec kiro-cli refuses. Kept as its own mapping rather than folded
# into the one above because the two answer different questions -- is this field
# the right shape, and are its contents the right shape -- and the second is
# applied per ITEM so one malformed name cannot discard the ones beside it.
_MANAGED_MCP_ENTRY_ITEM_TYPES: dict[str, type] = {
    "autoApprove": str,
    "disabledTools": str,
}

# Env keys a managed MCP server's spec must never carry through from
# agent.json are NOT enumerated here. agent.json is agent-writable (not in
# _SENSITIVE_HOME_DIRS / _WRITE_PROTECTED_HOME_PATHS) and every managed
# server's ``env`` is launched verbatim by kiro-cli as the child process's
# environment, so the filter has to be exactly the one env.sanitize_spec_env
# already applies for the probe: PREFIX-matched, case-insensitively, over
# Kiro Crew's whole reserved namespace plus the loader/interpreter channels.
#
# Delegating rather than restating is the point. This function exists so the
# ownership rules live in one place: hand-synced copies drift, a local frozenset
# of reserved NAMES would be a third copy of a rule env.py already owns, and a
# name list fails open for the next KIROCREW_ variable somebody adds -- one
# reachable case is KIROCREW_CLI, which mcp_cron._caller_is_cli() reads as
# "skip per-session ownership entirely".
# env.py states the reviewable property instead: a config cannot author our
# namespace. KIROCREW_HOME is stripped by that same namespace rule and then
# re-pinned below to the gateway's actual override, so ours is the only value
# that can reach the child.


# Env keys that decide where ``Path.home()`` points, and therefore where a
# managed shim resolves its data home when no ``KIROCREW_HOME`` pin is present.
# Stripped from a MANAGED entry only -- this is not a deny rule for specs in
# general, and env.sanitize_spec_env deliberately lets ``HOME`` through because a
# user's own MCP server legitimately needs it.
#
# The population is what makes stripping correct here. A managed server is ours:
# its command and args are ours, and it resolves OUR data home through
# config_dir() -> Path.home(). On a default install that inheritance is exactly
# right, which is why _managed_mcp_env() emits nothing there. Letting a
# config-declared HOME override it would relocate the shim's whole data home:
# cron_add would report success into a store the gateway never reads and the job
# would never run -- the same silent split _managed_mcp_env documents, arriving
# through a door that only opened once user env survived a clean rebuild.
#
# Both spellings are listed because Path.home() consults HOME on POSIX and
# USERPROFILE on Windows, and a spec is portable across both.
#
# Held upper-cased and compared against key.upper(), because sanitize_spec_env
# preserves each key's ORIGINAL case (out[key] = value) -- so a spec declaring
# "userprofile" arrives with that spelling and an exact-case pop would miss it
# while Windows, whose env names are case-insensitive, would still honour it.
# This mirrors the folding sanitize_spec_env already does for its own prefixes;
# the rest of the tree folds case at every one of these boundaries.
_HOME_DERIVING_ENV_KEYS: frozenset[str] = frozenset({"HOME", "USERPROFILE"})

# Env names that decide WHAT a managed shim executes, rather than how it behaves
# once running. Stripped from a MANAGED entry only, for the same reason as the
# home-deriving pair above and matched the same way (upper-cased, compared against
# key.upper()): a user's own MCP server may legitimately need any of these, and
# ``env.sanitize_spec_env`` therefore does not refuse them globally.
#
# A managed shim is OURS, and some of them are scripts whose shebang resolves
# their interpreter by NAME at exec time (``#!/usr/bin/env node`` -- see the note
# in ``name_grant``). So a config-authored value here does not tune our process,
# it chooses a different program for us to run:
#
# * ``PATH`` picks which ``node``/``python``/binary the shebang resolves to.
# * ``BASH_ENV`` and ``ENV`` name a file a non-interactive shell SOURCES first.
# * ``SHELLOPTS``/``BASHOPTS`` inject shell options the launcher never set.
# * ``NODE_OPTIONS`` carries ``--require``, and ``NODE_PATH`` redirects resolution.
#
# Grouped as one class deliberately. The interpreter half of this problem is
# already stated as a namespace in env.py (``PYTHON`` by prefix) because that
# population is unbounded; these have no shared prefix to key on, so they are
# enumerated -- and the enumeration is scoped to the launchers a managed shim
# actually uses (a shell, and node) rather than trying to cover every runtime.
_LAUNCHER_EXEC_ENV_KEYS: frozenset[str] = frozenset(
    {"PATH", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "NODE_OPTIONS", "NODE_PATH"}
)


def _mcp_registry_mode() -> bool:
    """True when the operator has declared this install registry-governed.

    An enterprise Kiro profile with an MCP Registry URL puts the client in
    `registry` access mode, where it resolves each `mcpServers` entry that
    carries ``"type": "registry"`` against the admin's catalog BY THE MAP KEY
    and silently drops every entry that does not. Without the marker the
    managed servers are filtered out before launch and the features they carry
    (`spawn_run`, `cron_add`, `learn_add`, ...) disappear with no local error.

    The mode cannot be auto-detected: the client fetches the toggle and the
    registry URL from GetProfile at startup and persists neither, so nothing on
    disk distinguishes a governed account from an ungoverned one. It is an
    explicit operator declaration, defaulting to false because the filter is
    symmetric — outside registry mode the marked entries are the dropped ones,
    so stamping unconditionally would break every personal install.

    Read through the EFFECTIVE config rather than ``config.json`` alone, because
    ``config.local.json`` deep-merges over it and is where ``kirocrew config set
    --local`` writes. Reading only the base file would ignore an overlay that
    declares the mode, emit no marker, and reproduce the silent drop this whole
    change exists to prevent.
    """
    try:
        # Function-local like the model resolver a few frames up: importing the
        # loader at module scope closes an import cycle through the config plane.
        from kiro_crew.config.loader import KiroCrewConfig

        return KiroCrewConfig.load().agent.mcp_registry_mode is True
    except Exception:
        # A config that cannot be loaded is not a governed declaration. Fall back
        # to the base file so a partially broken overlay still cannot flip the
        # marker on by accident.
        agent_mod.logger.debug("effective config unavailable for registry mode", exc_info=True)
        cfg = agent_mod._load_json(agent_mod._mc_config_path()) or {}
        agent_cfg = cfg.get("agent")
        if not isinstance(agent_cfg, dict):
            return False
        return agent_cfg.get("mcp_registry_mode") is True


def _mcp_spec_gate_open(name: str, spec: dict) -> bool:
    """Whether *spec*'s ``spec_gate`` permits emission RIGHT NOW (absent = open).

    The single place a gate is called. A gate that raises is reported CLOSED, for
    the same fail-closed reason the computer-use gate itself is: emitting the
    entry is what makes kiro-cli spawn the backend, and a keystone we could not
    read is not evidence that the capability is on.
    """
    gate = spec.get("spec_gate")
    if gate is None:
        return True
    try:
        return bool(gate())
    except Exception:
        agent_mod.logger.debug("spec gate for %s raised; treating as closed", name, exc_info=True)
        return False


def _mcp_server_emission_eligible(
    name: str, spec: object, *, gated_off: "frozenset[str] | None" = None
) -> bool:
    """Whether a FRESH spec build would EMIT this MCP server entry.

    THE single definition of "the rebuild re-adds this", and it has exactly two
    disqualifiers, both owned by the entry's own spec:

    * ``opt_in`` — an assignable set, never auto-emitted. ``build_agent_config``
      skips it outright and ``_refresh_dynamic_fields`` keeps an EXISTING grant
      current without ever re-introducing one, so nothing re-adds a grant the
      user removed.
    * a CLOSED ``spec_gate`` — both writers ``pop`` the entry while the gate is
      shut, so the rebuild actively withholds it rather than merely skipping it.

    Both spec writers consult this, and so does the dashboard PUT's merge-on-write
    host set (``handlers/agents.py::_app_or_host_owned``). That co-tenancy is the
    whole point of the helper rather than a convenience: the merge preserves an
    absent managed entry *because* a rebuild would re-add it, so if the two ever
    disagreed the merge would resurrect entries the rebuild withholds — an
    ``opt_in`` grant the user revoked through the only surface that can revoke it,
    or a gate-closed server whose backend the gate exists to keep unspawned.

    *gated_off* is a caller's ONE-PER-REBUILD gate snapshot
    (:func:`_gated_off_servers`); passing it keeps a rebuild's emit path and its
    withhold audit agreeing on one reading, which is why that snapshot exists.
    Omitted (the merge's case, which audits nothing), the gate is read live.

    A spec that is not a mapping at all is reported ELIGIBLE. Only the host can
    produce that shape — the managed map is a module constant and the extras come
    from an edition adapter — the name is host-owned either way, and this keeps
    the merge's pre-existing verdict for it instead of raising ``AttributeError``
    out of a commit unit contracted to leave its targets byte-identical.
    """
    if not isinstance(spec, dict):
        return True
    if spec.get("opt_in"):
        return False
    if gated_off is not None:
        return name not in gated_off
    return _mcp_spec_gate_open(name, spec)


def emission_eligible_mcp_servers() -> frozenset[str]:
    """Every MCP server name a fresh spec build would emit right now.

    Managed servers and the edition's extras under ONE predicate — extras get no
    exemption, so an extra that ever carries ``opt_in`` or a gate is withheld
    here for the same reason a managed one is. Today they carry neither, so this
    is every extra plus the always-emitted managed entries.

    Exported (no leading underscore) because ``handlers/agents.py``'s
    merge-on-write is a legitimate out-of-module consumer: it must preserve
    exactly the set a rebuild would re-add, and computing that itself is what let
    the two drift. Read live rather than cached — a keystone flip between two PUTs
    must change the answer.
    """
    return frozenset(
        name
        for name, spec in (
            *agent_mod._MANAGED_MCP_SERVERS.items(),
            *agent_mod._extra_mcp_servers().items(),
        )
        if _mcp_server_emission_eligible(name, spec)
    )


def crew_owned_mcp_servers() -> frozenset[str]:
    """Every MCP server name Crew owns, whether or not a rebuild would emit it.

    Deliberately NOT :func:`emission_eligible_mcp_servers`. That set answers
    "would a rebuild re-add this", so it drops every ``opt_in`` entry — and an
    ``opt_in`` server the user DID grant is in their spec, serving tools, which is
    exactly a server a caller asking this question needs named. Asking the
    eligible set instead would silently omit the granted ones
    (``kirocrew-dashboard``, ``kirocrew-work``, ``kirocrew-crew-log``).

    The inverse error is harmless, which is why this errs wide: a consumer matches
    these names against the servers a spec actually carries, so a name for a
    server that is absent matches nothing. Naming one costs nothing; missing one
    is the defect.

    The edition seam's extras are included, and that is not an accident: they come
    from ``_extra_mcp_servers()``, an edition ADAPTER, not from user config, so they
    are host-owned in exactly the sense the managed map is. Whatever an edition
    contributes there is Crew's own server and belongs in this set; a user's own
    ``mcp.json`` entry can never reach it. The names are not constrained to a
    ``kirocrew-`` prefix, so no caller may assume one.
    """
    return frozenset((*agent_mod._MANAGED_MCP_SERVERS, *agent_mod._extra_mcp_servers()))


def _gated_off_servers() -> frozenset[str]:
    """Managed servers whose ``spec_gate`` is CLOSED right now.

    Evaluated ONCE per rebuild and threaded through the emit path and the withhold
    audit, rather than each re-reading the gate. The reads are cheap; agreeing is
    the point. A keystone flip landing between the two would produce a spec and an
    audit trail that contradict each other — the record claiming a server was
    withheld when it was emitted, or staying silent when it was withheld. That
    record is read during incident response, against the config it describes.

    A gate that raises is treated as closed, for the same fail-closed reason the
    computer-use gate itself is — see :func:`_mcp_spec_gate_open`, which is where
    that call now lives so the merge-on-write host set reads the gate the same way.
    """
    return frozenset(
        name
        for name, spec in agent_mod._MANAGED_MCP_SERVERS.items()
        if not _mcp_spec_gate_open(name, spec)
    )


def managed_mcp_spec_entry(name: str, *, include_opt_in: bool = False) -> dict[str, Any] | None:
    """The kiro-spec ``mcpServers`` entry a fresh build would emit for *name*.

    One entry, resolved live (``invocation_fn`` + the pinned data home), for a
    consumer that needs a single managed server without rebuilding the whole
    config. ``None`` when *name* is not managed, when it is ``opt_in`` (an
    assignable set is granted by a spec, never minted here) or when its
    ``spec_gate`` is closed — the same predicate the two spec writers use, so a
    caller cannot resurrect a server emission withholds.

    ``include_opt_in`` resolves an ``opt_in`` entry's invocation anyway, and exists
    for the ONE caller that is not asking the emission question:
    ``mcp_gateway.gatewayd._spawns_own_control_plane``, which compares a spawn's
    binary and argv against the invocation this name is DEFINED as. A closed
    ``spec_gate`` still yields ``None`` under the flag; the branch below says why the
    two disqualifiers part company there. It grants nothing on its own, because
    neither spec writer passes it: an opt-in entry a writer omits is still omitted.

    ``autoApprove`` is deliberately NOT carried, unlike the emit loop in
    :func:`build_agent_config`. The flag is kiro-cli's local approval, and the
    one caller here (the claude MCP translation, :mod:`kiro_crew.acp.session_mcp`)
    targets a backend whose nearest equivalent — a ``permissions.allow`` entry —
    means Claude never asks, so the call never reaches Crew's gate. Emitting the
    entry un-approved keeps every call gated.

    Never raises: an invocation that cannot be resolved yields ``None``, because
    the caller is on a spawn path where no MCP server is better than no session.
    """
    spec = agent_mod._MANAGED_MCP_SERVERS.get(name)
    if not isinstance(spec, dict):
        return None
    if include_opt_in:
        # The control-plane check asks what this name's INVOCATION is, not whether
        # a rebuild would GRANT it, and ``_mcp_server_emission_eligible`` answers
        # the second question. Its two disqualifiers part company here: ``opt_in``
        # means "never auto-emitted, assigned per agent", so an opt-in server that
        # IS running was legitimately granted and its invocation is still ours to
        # compare against; a CLOSED ``spec_gate`` means the opposite -- the gate
        # exists to keep that backend unspawned, so a spawn under its name is
        # anomalous and must not be handed a token. Skip the first, keep the second.
        if not _mcp_spec_gate_open(name, spec):
            return None
    elif not _mcp_server_emission_eligible(name, spec):
        return None
    try:
        if "invocation_fn" in spec:
            cmd, args = spec["invocation_fn"]()
        else:
            cmd = spec.get("command") or spec["command_fn"]()
            args = list(spec["args"])
    except Exception:
        agent_mod.logger.warning(
            "cannot resolve invocation for managed MCP server %r", name, exc_info=True
        )
        return None
    if not cmd:
        return None
    entry: dict[str, Any] = {"command": cmd, "args": list(args)}
    env = _managed_mcp_env()
    if env:
        entry["env"] = env
    return entry


def _enforce_managed_mcp_ownership(
    entry: dict,
    spec: dict,
    registry_mode: bool,
    *,
    auto_approve: str,
) -> None:
    """Strip/re-pin the fields Kiro Crew owns on one managed-server entry.

    Applied identically by the fresh-build path (``build_agent_config``) and
    the existing-config refresh path (``_refresh_dynamic_fields``) so the two
    cannot hand-drift: a silent divergence between two copies of this same
    ownership logic lets a clean rebuild discard a user's timeout/env, and
    would just as easily reopen a security-relevant strip (e.g. the
    reserved-env-key scrub below) if only one of the two loops picked it up.

    ``entry["command"]``/``entry["args"]`` are set by the caller beforehand —
    both loops resolve those slightly differently (build vs. refresh), so
    ownership of that resolution stays with them.

    ``auto_approve`` names what should happen to ``autoApprove``, as ONE
    parameter rather than a pair of booleans, so a caller cannot spell a
    combination that has no meaning. The three values are the three real cases:

    * ``"own"`` (build) -- ``autoApprove`` tracks the spec on every call, like
      every other field this function owns, so agent- or user-written
      ``autoApprove`` data never survives a clean build.
    * ``"seed"`` (refresh, entry absent) -- set it from the spec, because a
      server the user does not have yet has no preference to respect.
    * ``"preserve"`` (refresh, entry present) -- leave it alone, so a user who
      deliberately removed a grant is not silently handed it back on every
      refresh.
    """
    # Entry keys are an ALLOW-LIST, not a list of known-bad names. kiro-cli
    # rejects a server spec carrying a field it does not know, and it rejects the
    # WHOLE agent when it does, so a single stray ``cwd`` on a managed entry
    # takes every Kiro Crew tool down with it. Dropping the key we cannot honour
    # keeps the agent loadable, which is what the user actually wanted.
    #
    # It has to be an allow-list because the deny side is unbounded: ``url`` and
    # ``headers`` were the two stale fields older builds left behind (these
    # servers are stdio-only, and a leftover ``url`` would shadow the command and
    # propagate into the CC config), but naming them one at a time fails open for
    # the next field anybody hand-writes. Both are absent from the set below, so
    # they are still dropped -- now as instances of a rule rather than as two
    # names.
    #
    # Stray keys reach HERE because the fresh-build path preserves the user's
    # timeout/env instead of rebuilding the entry from scratch and discarding
    # every unknown key along with the rest.
    for stray_key in [k for k in entry if k not in _MANAGED_MCP_ENTRY_KEYS]:
        entry.pop(stray_key, None)
        agent_mod.logger.warning(
            "dropping %r from a managed MCP server's entry: it is not a field a "
            "managed entry may carry, and kiro-cli rejects the whole agent spec "
            "over one unknown field",
            stray_key,
        )
    # A right key with a wrong-TYPED value fails exactly the same way: the spec is
    # schema-checked, so ``"disabled": "false"`` (a string where a boolean belongs)
    # loses the user every Crew tool just as surely as an unknown field. Dropping
    # the ill-typed value rather than coercing it is the same call already made one
    # level down for env ENTRIES in ``sanitize_spec_env``: a value we invent is not
    # the one the user wrote, and the rest of their entry still survives.
    #
    # Only the keys a user may author are checked. ``command``/``args`` are set by
    # both callers before this runs, so their types are ours, not input. Unlike
    # the env-NAME case, this list is closed and finite -- exactly the fields
    # ``_MANAGED_MCP_ENTRY_VALUE_TYPES`` names, with the types the same schema
    # documents -- so it converges instead of growing a name per round.
    for typed_key, expected in _MANAGED_MCP_ENTRY_VALUE_TYPES.items():
        if typed_key not in entry:
            continue
        value = entry[typed_key]
        # ``bool`` is a subclass of ``int``, so a bare isinstance check would let
        # ``"timeout": true`` through as a number.
        wrong_type = not isinstance(value, expected) or (
            expected is not bool and isinstance(value, bool)
        )
        # A float can be the right TYPE and still not be representable. Python's
        # json module accepts bare ``NaN``/``Infinity`` on the way in and writes
        # them back out verbatim, but neither is JSON, so a strict parser rejects
        # the file -- and kiro-cli rejects the whole agent with it. isfinite is the
        # complete test here rather than another name to remember: it covers NaN,
        # +Infinity and -Infinity, which is every non-finite float there is.
        if not wrong_type and isinstance(value, float) and not math.isfinite(value):
            wrong_type = True
        if wrong_type:
            entry.pop(typed_key, None)
            agent_mod.logger.warning(
                "dropping %r from a managed MCP server's entry: %r is not the "
                "type kiro-cli's spec schema documents for it, and it would be "
                "rejected along with the whole agent",
                typed_key,
                value,
            )
    # A list of the right TYPE can still hold the wrong ITEMS. Both list-valued
    # keys are documented as arrays of tool NAMES, so ``disabledTools: [1]``
    # satisfies the check above and still emits a spec kiro-cli refuses -- the
    # container was validated and its contents were not.
    #
    # Filtered per ITEM rather than dropped whole, which is the rule this fix
    # already applies one level down to env ENTRIES in ``sanitize_spec_env``:
    # a well-typed sibling survives its neighbour. That matters most for
    # ``disabledTools``, where discarding the list because one item is malformed
    # would re-expose every tool the user did name correctly.
    for list_key in _MANAGED_MCP_ENTRY_ITEM_TYPES:
        items = entry.get(list_key)
        if not isinstance(items, list):
            continue
        kept = [item for item in items if isinstance(item, str)]
        for bad_item in [item for item in items if not isinstance(item, str)]:
            agent_mod.logger.warning(
                "dropping %r from a managed MCP server's %r: that list carries "
                "tool names, and a non-string item would be rejected along with "
                "the whole agent spec",
                bad_item,
                list_key,
            )
        if len(kept) != len(items):
            entry[list_key] = kept
    # Enterprise registry MARKER — the ``registry`` value is ours and tracks the
    # account the gateway is actually signed in to, so it is set when the
    # declaration is on and removed (not left stale) when it is off, which stops
    # a host that leaves an enterprise profile from shipping a marker the inverse
    # filter would now use to drop these servers. Only that value: a transport
    # hint the user wrote is theirs and is carried through untouched.
    if registry_mode:
        entry["type"] = _MCP_REGISTRY_TYPE
    elif entry.get("type") == _MCP_REGISTRY_TYPE:
        entry.pop("type", None)
    # Env: keep the user's own variables, but only genuine ones. A malformed
    # (non-dict) override is dropped rather than fed to dict(...), which would
    # otherwise raise and abort the whole config rebuild over a single bad
    # agent.json value. sanitize_spec_env then drops Kiro Crew's whole reserved
    # namespace and the loader/interpreter channels by prefix (see the module
    # comment above), the home-deriving names are dropped for this population
    # (see _HOME_DERIVING_ENV_KEYS), and KIROCREW_HOME is re-pinned to the
    # gateway's actual override afterwards so ours is the value that reaches the
    # child.
    existing_env = entry.get("env")
    env = sanitize_spec_env(existing_env.items()) if isinstance(existing_env, dict) else {}
    for home_key in [k for k in env if k.upper() in _HOME_DERIVING_ENV_KEYS]:
        env.pop(home_key, None)
        agent_mod.logger.warning(
            "dropping %r from a managed MCP server's env: it would move the "
            "data home this shim shares with the gateway",
            home_key,
        )
    for exec_key in [k for k in env if k.upper() in _LAUNCHER_EXEC_ENV_KEYS]:
        env.pop(exec_key, None)
        agent_mod.logger.warning(
            "dropping %r from a managed MCP server's env: it would choose what "
            "this shim executes rather than configure it (see "
            "_LAUNCHER_EXEC_ENV_KEYS)",
            exec_key,
        )
    env.update(_managed_mcp_env())
    if env:
        entry["env"] = env
    else:
        entry.pop("env", None)
    if auto_approve == "own":
        if "autoApprove" in spec:
            entry["autoApprove"] = list(spec["autoApprove"])
        else:
            # agent.json is agent-writable; it cannot grant auto-approval to a
            # managed server that does not ship an audited default grant.
            entry.pop("autoApprove", None)
    elif auto_approve == "seed" and "autoApprove" in spec:
        entry["autoApprove"] = list(spec["autoApprove"])


def emit_managed_servers(mcp: dict, *, gated_off: frozenset[str], registry_mode: bool) -> None:
    """Emit every eligible managed server into a freshly built ``mcpServers`` map."""
    for name, spec in agent_mod._MANAGED_MCP_SERVERS.items():
        if not _mcp_server_emission_eligible(name, spec, gated_off=gated_off):
            # NOT eligible, and the two reasons part company on one point: a
            # closed gate must RETRACT the entry, an opt-in one is merely never
            # introduced.
            if name in gated_off:
                # The gate is the whole point of this branch: emitting the entry is
                # what makes kiro-cli spawn the backend, so a closed gate must not
                # emit one. ``pop`` as well as ``continue`` because the base here is
                # shipped defaults merged with the user override file, and an entry
                # arriving from there would otherwise slip past a platform gate that
                # exists because the capability has no driver on this OS.
                mcp.pop(name, None)
            # An opt-in server is an assignable set: it belongs to the agents whose
            # own spec references it, so a freshly built default spec must not carry
            # it. kiro-cli loads a server only when ``tools`` names it, and the
            # shipped template names only the always-on ones. Left in place rather
            # than popped: an entry already on disk is a grant the user made.
            continue
        if "invocation_fn" in spec:
            cmd, args = spec["invocation_fn"]()
        else:
            cmd = spec.get("command") or spec["command_fn"]()
            args = list(spec["args"])
        # The deep merge above may have supplied user-owned options such as a
        # timeout or extra environment variables. Keep those on a clean build,
        # while replacing the invocation and transport fields that define our
        # trusted managed server. Ownership of every other field is enforced
        # by the same helper the refresh path uses (_enforce_managed_mcp_ownership),
        # so the two loops cannot silently diverge on what counts as "ours".
        existing = mcp.get(name)
        entry = dict(existing) if isinstance(existing, dict) else {}
        entry["command"] = cmd
        entry["args"] = args
        _enforce_managed_mcp_ownership(entry, spec, registry_mode, auto_approve="own")
        mcp[name] = entry


def refresh_managed_servers(mcp: dict, *, gated_off: frozenset[str], registry_mode: bool) -> None:
    """Keep an existing spec's managed servers current without granting an opt-in set."""
    for name, spec in agent_mod._MANAGED_MCP_SERVERS.items():
        eligible = _mcp_server_emission_eligible(name, spec, gated_off=gated_off)
        if not eligible and name in gated_off:
            # RETRACT, not merely skip: an earlier refresh wrote this entry while
            # the gate was open, and leaving it would mean turning the feature
            # off never reclaims the backend process turning it on started.
            #
            # The entry's user-owned fields are NOT preserved. Stashing them
            # would need an agent-writable sidecar, and an ``autoApprove``
            # restored from there is a self-granted auto-approve: kiro-cli
            # approves such a tool locally, so ``hooks.on_tool_call`` never sees
            # the call. An off/on cycle therefore resets a customized entry and
            # the operator re-applies it — losing an approval is the safe
            # direction, granting one from an agent-writable file is not.
            #
            # The server's ``@ref`` in ``tools`` is deliberately left alone. A ref
            # whose server has no ``mcpServers`` entry resolves to nothing and
            # mounts nothing, so withholding the entry is the whole control;
            # removing the ref as well would destroy a grant the user may have
            # narrowed by hand and cannot be reconstructed on re-enable.
            mcp.pop(name, None)
            continue
        is_new = name not in mcp
        # An opt-in server is granted by the spec itself, so a refresh keeps an
        # entry the user put there current but never introduces one: adding it
        # back would re-grant a set on every gateway start. Spelled through the
        # shared eligibility predicate (the gate half is already spent above, so
        # what remains of ineligibility here is exactly ``opt_in``) rather than
        # re-reading the flag, so the rule cannot drift from the emitter's or the
        # dashboard merge's reading of it.
        if is_new and not eligible:
            continue
        if not is_new and spec.get("opt_in") and not isinstance(mcp.get(name), dict):
            # A hand-written entry that is not an object at all. Refreshing it
            # would raise (item assignment on a str), and rewriting it would
            # discard whatever the user meant to say. Leave it untouched and let
            # doctor report it — this pass repairs OUR fields, it does not
            # adjudicate malformed user input.
            #
            # Only for an OPT-IN server, whose entry the user hand-wrote. An
            # always-on entry is ours, nobody hand-writes it, and a malformed one
            # deliberately falls through to raise: the caller catches TypeError
            # and rebuilds from defaults, which is what restores the server.
            # Skipping it here instead would leave it malformed, so validation
            # drops it while its ``@ref`` stays in ``tools`` — every tool on that
            # server silently gone.
            continue
        entry = mcp.setdefault(name, {})
        if "invocation_fn" in spec:
            entry["command"], entry["args"] = spec["invocation_fn"]()
        else:
            entry["command"] = spec.get("command") or spec["command_fn"]()
            entry["args"] = list(spec["args"])
        # Strip any stale remote-transport fields from older builds, re-pin the
        # registry marker and data-home env, and seed autoApprove only for a
        # genuinely new entry — all via the same helper the fresh-build loop
        # uses, so the two ownership rules cannot hand-drift.
        _enforce_managed_mcp_ownership(
            entry, spec, registry_mode, auto_approve="seed" if is_new else "preserve"
        )


def register_managed_refs(config: dict, *, fresh_install: bool, gated_off: frozenset[str]) -> None:
    """Reference the managed servers in ``tools`` and audit the ones a gate withholds."""
    valid_servers: dict[str, Any] = config["mcpServers"]
    # On fresh installs, ensure managed MCP tools are in tools (but NOT
    # allowedTools — new MCPs may have destructive tools; user opts in).
    # On existing configs, don't touch tools/allowedTools — user controls those.
    if fresh_install:
        added_refs: list[str] = []
        # Managed servers + edition-contributed servers both get their @ref
        # registered so their tools are callable. Edition servers are injected
        # into config['mcpServers'] via _extra_mcp_servers() above; their @ref
        # must also be added to config['tools'], otherwise kiro-cli exposes the
        # server but not its tools. The public edition contributes none, so this
        # is a no-op there.
        _register_names = list(agent_mod._MANAGED_MCP_SERVERS) + [
            n for n in agent_mod._extra_mcp_servers() if n not in agent_mod._MANAGED_MCP_SERVERS
        ]
        for mcp_name in _register_names:
            ref = f"@{mcp_name}"
            if mcp_name in valid_servers and ref not in config.get("tools", []):
                config.setdefault("tools", []).append(ref)
                added_refs.append(ref)
        if added_refs:
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="install_agent",
                resources=f"{', '.join(added_refs)} added to tools (fresh install)",
            )

    # Narrow ADD-only exception on EXISTING configs, mirroring the
    # ``tool_search`` precedent in _refresh_dynamic_fields: ensure the
    # computer-use @ref is in ``tools``.
    #
    # Without this, an UPGRADING install never gains the ref — the fresh-install
    # branch above is the only place it is added — so ``kirocrew-computer`` is
    # registered in ``mcpServers`` but kiro-cli exposes none of its tools, and the
    # feature silently does nothing for every pre-existing user. (Unlike a
    # third-party MCP, the user cannot have "opted out" of a ref that never
    # existed on their install.)
    #
    # DELIBERATELY tools-only, never ``allowedTools``: that list is kiro-cli's
    # blanket auto-approve, and an auto-approved MCP tool is approved locally by
    # kiro-cli — it emits no permission request, so ``hooks.on_tool_call`` (the
    # deny floor + governance ceiling + approval clamp) is never reached for it.
    # Granting it here would delete the PreToolUse plane for a tool that can click
    # and type into an already-authenticated application.
    #
    # Gated on the shipped template actually granting the ref (so an edition that
    # drops computer use is respected) and on the server having resolved, and
    # scoped to this ONE server so no other managed ref is re-added behind the
    # user's back. The primary enable still lives in the keystone file, so a config
    # that gains the ref is not a feature that turns itself on: the shim answers an
    # empty tools/list until the user opts in from Settings.
    if not fresh_install and CU_MCP_SERVER in valid_servers:
        cu_ref = f"@{CU_MCP_SERVER}"
        shipped_tools = agent_mod.get_shipped_tools().get("tools", [])
        existing_tools = config.get("tools")
        if (
            isinstance(existing_tools, list)
            and cu_ref in shipped_tools
            and cu_ref not in existing_tools
        ):
            existing_tools.append(cu_ref)
            agent_mod.sel().log_api_access(
                caller="system",
                operation="mcp_tools_added",
                outcome="ok",
                source="install_agent",
                resources=f"{cu_ref} added to tools (existing config upgrade)",
            )

    # Audit the DECISION, not a config delta. Nothing in the spec changes shape
    # when a gate closes — the ``@ref`` stays exactly where the template put it
    # and only the ``mcpServers`` entry is withheld — so there is no delta to
    # observe, and a reader of the audit trail would otherwise have no record
    # that a shipped server was deliberately not emitted. Derived from the gate
    # plus the shipped template so the fresh and existing paths record the same
    # fact.
    _withheld = sorted(
        f"@{name}"
        for name in gated_off
        if f"@{name}" in agent_mod.get_shipped_tools().get("tools", [])
    )
    if _withheld:
        agent_mod.sel().log_api_access(
            caller="system",
            operation="mcp_server_withheld",
            outcome="ok",
            source="install_agent",
            resources=(
                f"{', '.join(_withheld)} withheld from mcpServers (unsupported "
                f"platform or capability disabled); its tools ref is retained and "
                f"resolves to nothing"
            ),
        )


def _managed_opt_in_entry(subcommand: str) -> dict[str, Any]:
    """One hand-built ``mcpServers`` entry for an ``opt_in`` managed server.

    Neither spec-writing loop emits an opt-in server, so every installer that
    grants one builds the entry itself — and the two fields that are easy to
    forget are why this is a helper rather than three copies. Without
    ``"type": "registry"`` a registry-mode client silently DROPS the entry, so the
    granted tools never launch and the grant is dead with no local error; without
    the ``KIROCREW_HOME`` pin the shim reads the DEFAULT data home while the
    gateway runs under an override, so the tools would act on a different store
    than the one the session reports on. Both helpers return empty on a default
    install, so the emitted spec is unchanged there.
    """
    command, args = agent_mod._kirocrew_mcp_invocation(subcommand)
    entry: dict[str, Any] = {"command": command, "args": args}
    if _mcp_registry_mode():
        entry["type"] = _MCP_REGISTRY_TYPE
    env = _managed_mcp_env()
    if env:
        entry["env"] = env
    return entry

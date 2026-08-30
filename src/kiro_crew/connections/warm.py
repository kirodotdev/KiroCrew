"""The shared warm mint: the specs one kiro-cli process serves, and the rows it holds.

The cold path (:mod:`kiro_crew.connections.mint`) pays a full kiro-cli spawn PER provider
for one approval URL. The warm design serves every card's URL from ONE process instead:
mode activation costs a FIXED ~5.18s whether the spec carries one remote server or six, so
a spec holding every mintable provider yields every ``oauth_request`` in a SINGLE
activation.

Three halves have landed. The TABLE: the row shape a shared mint uses (``shared`` /
``generation`` / ``activation`` on :class:`~kiro_crew.connections.mint.MintState`) and the
withdrawal chokepoint the dashboard's status path calls (:func:`expire_dead_mints`). The
SPECS: the registry scan deciding which providers a warm process could serve
(:func:`warm_spec_providers`), the plan it would spawn on (:func:`_warm_spec_plan`), and
the spec files that plan writes (:func:`_write_warm_mint_specs`). The RUNTIME: the one
process every card shares (:class:`_WarmMintRuntime`) -- spawned, activated, parked, killed
and reaped (:func:`_warm_mint_reaper`) -- and the flow that turns one activation into a
whole table of URLs (:func:`warm_mint_all`).

Redeemability takes TWO questions and they die independently: the PKCE verifier lives in
the PROCESS (``generation_is_live``) while the loopback listener answering the redirect
belongs to the SESSION (``activation_is_live``). Process liveness alone passed a
terminated-session row, which is how a card kept serving an unredeemable URL. Both
failures are recorded in ``docs/architecture/design-notes/connections-warm-table.md``.

Four rules are load-bearing and recorded in that same note: the SESSION is HELD (see
:func:`_warm_row_alive`), specs are enumerated ONCE at spawn so a plan that merely SHRANK
must not force a respawn and a process still holding a consent is PARKED rather than killed
(:func:`_plan_is_servable`, :meth:`_WarmMintRuntime._park_or_kill_locked`), the spec
universe is registry-derived and BLIND to grant and cancel state because a plan tracking
who needs a URL now retires a process holding other cards' listeners, and a warm session
injects an EMPTY ``mcp_servers`` list because remote servers passed through ``session/new``
kill the process with every pending verifier in it
(:func:`_warm_session_mcp_servers`).

IDENTITY: a row is fenced by its own opaque ``token``, never by the batch clock reading in
``started``. ``time.monotonic()`` has ~15.6ms granularity on Windows, so two Connects for
one provider inside a tick read as one row and a late absorb overwrites the newer claim --
the same reasoning
:func:`~kiro_crew.connections.mint._new_mint_token` records for the cold engine. WITHDRAWAL
is the other axis and does NOT use the token: a row is expired because the process that
holds its verifier is gone, so :func:`_expire_shared_mints` narrows by ``generation`` only.

ATOMICITY: :func:`_claim_shared_mints` contains no await, so a caller either holds every
claim it asked for or none -- the claim is taken before :func:`warm_mint_all` enters the
``try`` that rolls it back, which makes any await in that loop an unprotected cancel window.
The rows a claim displaced come back to the caller and are disposed inside that ``try``.

CANCELLATION SAFETY is the invariant this module is written around, because two review
rounds found the same bug class: an await sitting between a state mutation and that
mutation's settlement or cleanup, guarded only by an ``except Exception`` that a
``CancelledError`` walks straight past. Every such window is closed the same way -- the
mutation is either atomic by construction (no await in it) or its cleanup is a ``finally``
/ ``except BaseException`` that re-raises. Concretely: a claim rolls back
(:func:`warm_mint_all`), an activation's session is ALWAYS settled so the sweep can collect
it (:func:`_absorb_warm_requests`), and a process whose teardown did not finish stays
PARKED with a drain armed rather than losing its only reference
(:meth:`_WarmMintRuntime._park_or_kill_locked`, ``_sweep_retiring_locked``,
``_retire_locked``, ``_abandon_spawn_locked``). Pinned by an AST guard in
``test/test_connections_warm.py``, not merely described here.

SESSION OWNERSHIP is explicit at every transfer, because the handle is the ONLY way to
terminate a backend session and the loopback callback children it owns. One rule covers
every point: a handle is REGISTERED in ``_sessions`` before anything can be interrupted, and
FORGOTTEN only once its destroy has completed -- so an interrupted teardown leaves a record
the ordinary sweep retries. The create is run as a shielded task so its handle stays
reachable even when the wait for it is abandoned
(:meth:`_WarmMintRuntime._abandon_session_creation_locked`). When no handle can be recovered
at all the generation is QUARANTINED rather than trusted to retire on its own, which bounds
the unaddressable residual to one generation's sessions -- retirement is not guaranteed to
arrive, because a card holding a URL keeps the reaper's idle clock reset while the digest fast
path keeps the same process reusable.

LIVENESS is asked of the parked list too. A generation keeps its NUMBER while parked -- only
a successful spawn bumps the counter -- so ``generation_is_live`` uses the equality test to
CONFIRM liveness and never to deny it, then falls through to ``_retiring``. Denying on
equality reported a stood-down-but-alive process dead, which withdrew redeemable URLs and
then let the next sweep kill the process the park existed to preserve.

OWNERSHIP is checked twice, because a spec is activated BY NAME. The writer refuses a path
whose contents this module did not write, which protects the FILE; :func:`_unowned_plan_specs`
then re-verifies every planned spec exists and is ours BEFORE the runtime is constructed,
which protects the SPAWN. Without the second check the refusal would hand kiro-cli a
stranger's spec at our fixed name and initialize its ``mcpServers``. A refusal aborts
warming entirely, audited: the cold path still serves every Connect.

OWNERSHIP: these specs carry FIXED, predictable names in the user's own agents directory,
so a name is where a spec of ours would GO and never proof that the file there is one.
Every spec written here is stamped with :data:`_WARM_SPEC_SENTINEL` on its description --
the stock defaults a spec body also fixes are values a user's own agent plausibly carries,
so the sentinel is what actually discriminates. Neither :func:`_write_warm_mint_specs` nor
:func:`_remove_warm_mint_specs` unlinks or overwrites a path whose contents this module did
not write -- see :func:`_warm_spec_is_foreign`.

INVARIANT: no coroutine here touches the filesystem directly. The spec helpers read the
user's config, the shared agents dir, or kiro-cli's OAuth cache, and the credential gate
reads the operator's OAuth-endpoint extension -- any of which can sit on a network mount
where a stat is unbounded -- so they are synchronous, and a coroutine reaches them through
``asyncio.to_thread``. Enforced by a fixed-point drift guard in
``test/test_connections_warm.py``, not merely described here.

REQUEST PATH: :func:`warm_mint_all` is driven by ``POST /api/connections/premint``, which the
Connections page fires once on mount. The endpoint scans the candidates and hands them over,
so the slugs it reports and the rows the engine claims come from one registry read. Proactive
refresh attaches in :func:`_warm_mint_reaper` when slice N3 lands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import agent as _agent
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.config.loader import data_home
from kiro_crew.connections.mint import (
    _MINT_AGENT_PREFIX,
    _MINT_GRANT_POLL_SECONDS,
    _MINT_NAME_RE,
    _MINT_TTL_SECONDS,
    MintState,
    _dispose_mint,
    _mint_spec_body,
    _mint_watcher,
    _mints,
    _mints_lock,
    _new_mint_token,
)
from kiro_crew.connections.registry import Provider, get_visible_providers
from kiro_crew.connections.tool_aliases import declared_tool_aliases, resolve_tool_aliases
from kiro_crew.mcp_discovery import list_servers
from kiro_crew.mcp_grant import grant_presence as grant_present
from kiro_crew.mcp_utils import (
    kiro_entry_client_id,
    kiro_entry_scopes,
    kiro_oauth_wire_entry,
    mcp_server_alias,
)
from kiro_crew.security import oauth_url_contains_credential
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Warm specs are FIXED names under the cold mint's prefix, so one glob finds them all. They
#: carry no ``-<pid>-<8hex>`` suffix, which keeps them out of the cold engine's manifest sweep
#: -- and is the only thing telling a warm spec from a cold one whose server is literally named
#: ``warm-*`` (see ``_is_stale_warm_spec``). The character class MUST match the cold engine's: a
#: case only ONE pattern accepts reads as ours and gets its live spec unlinked.
_WARM_AGENT_PREFIX = f"{_MINT_AGENT_PREFIX}warm-"
_WARM_NAME_RE = re.compile(rf"^{re.escape(_WARM_AGENT_PREFIX)}[a-z0-9_.-]+$")
_WARM_BASE_AGENT = f"{_WARM_AGENT_PREFIX}base"
_WARM_ALL_AGENT = f"{_WARM_AGENT_PREFIX}all"
#: Prefixed onto the ``description`` of every spec this module writes, and the mark that makes
#: ownership provable. The other fields the writer fixes (``model``, ``includeMcpJson``,
#: ``prompt``, ``allowedTools``) are STOCK DEFAULTS a hand-written or scaffolded agent
#: plausibly carries, so on their own they judged a user's own spec at a warm path as ours.
#: ``description`` is the only schema-legal field free enough to carry a marker: kiro-cli
#: rejects an unknown spec key, and the agent-spec migration sweep strips bookkeeping keys.
_WARM_SPEC_SENTINEL = "Kiro Crew warm mint spec (machine-written; safe to delete)"
_WARM_SPAWN_TIMEOUT_SECONDS = 90.0
_WARM_SESSION_TIMEOUT_SECONDS = 90.0
_WARM_SESSION_DESTROY_TIMEOUT_SECONDS = 10.0
#: How long to keep waiting for a handle whose create we already gave up on. Short, because
#: this only buys back a session already on its way; see ``_abandon_session_creation``.
_WARM_SESSION_REAP_TIMEOUT_SECONDS = 10.0
_WARM_KILL_TIMEOUT_SECONDS = 20.0
#: The oauth_request frame lands a beat AFTER set_mode returns (~0.35s measured),
#: beyond drain_init's idle window for a slow provider. Poll rather than race it.
_WARM_OAUTH_SETTLE_SECONDS = 0.5
_WARM_OAUTH_SETTLE_ROUNDS = 6
#: A tenth of the mint TTL: long enough that reopening the gallery reuses the
#: process, short enough that an abandoned visit leaves no kiro-cli resident.
_WARM_IDLE_GRACE_SECONDS = _MINT_TTL_SECONDS / 10
#: One respawn, then the cold path -- a second death means the process cannot stay
#: up, and a Connect is better served by its own dedicated spawn.
_WARM_ACTIVATION_ATTEMPTS = 2

#: The row states a shared mint is still working on. ``minting`` counts: a claim with no
#: URL yet is exactly what a cancelled activation must not leave behind.
_LIVE_STATES = ("minting", "waiting")


class _WarmMintUnsafe(RuntimeError):
    """A warm mint was about to be issued in a way that kills the shared process."""


class _WarmMintDied(RuntimeError):
    """The shared process was gone by the end of an activation."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _acp_runtime_factory() -> Any:
    """Indirection so tests can substitute a fake runtime class."""
    return AcpRuntime


def _warm_session_mcp_servers() -> list[dict[str, Any]]:
    """The session-injected MCP servers for a warm mint: ALWAYS empty.

    A remote server passed through ``session/new`` kills the process together with every
    pending verifier in it, so the spec -- never the session -- is what mounts servers.
    """
    return []


def _log_warm_event(operation: str, resources: str, outcome: str = "ok") -> None:
    """Record a warm-table event. Never carries a URL or an exception message."""
    sel().log_api_access(
        caller="dashboard",
        operation=operation,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


def connections_tool_aliases(server_aliases: list[str]) -> dict[str, str]:
    """``toolAliases`` for a spec mounting ``server_aliases``, or ``{}``.

    kiro-cli exposes MCP tool names RAW, so two mounted servers exporting the same
    name leave only one reachable. The collision set is DECLARED by the registry and
    resolved by :func:`resolve_tool_aliases`, so it is known before consent -- the
    MCP inventory carries no tool list for a server that never authorized.

    KEY SHAPE. The resolver keys by registry SLUG (``@slug/tool``) while this spec mounts
    servers under ``mcp_server_alias(slug)``, and where the two differ kiro-cli applies no
    rename and the collision comes back silently -- so keys are re-pointed at the MOUNTED
    alias here. Every registry slug is slash-free today, making this an identity map that
    holds the shape contract of the spec we WRITE rather than fixing a reachable bug; the
    design note's "Tool-alias key shape" carries the full reasoning.
    """
    declared = declared_tool_aliases()
    wanted = set(server_aliases)
    mounted = {slug: alias for slug in declared if (alias := mcp_server_alias(slug)) in wanted}
    resolved = resolve_tool_aliases(
        {slug: set(tools) for slug, tools in declared.items() if slug in mounted}
    )
    aliased: dict[str, str] = {}
    for ref, alias in resolved.items():
        # rpartition, not partition: a registry slug may itself contain a slash while a tool
        # name never does, so the LAST separator reliably splits server from tool.
        slug, _, tool = ref.lstrip("@").rpartition("/")
        aliased[f"@{mounted.get(slug, slug)}/{tool}"] = alias
    return dict(sorted(aliased.items()))


def _warm_spec_description(detail: str) -> str:
    """Every description this module writes: the sentinel, then the caller's detail.

    Sentinel-FIRST rather than appended, so the judge tests a prefix. A suffix could be
    truncated by any writer that clips the field, and a prefix a user would have to type
    verbatim to impersonate.
    """
    detail = detail.strip()
    return f"{_WARM_SPEC_SENTINEL} {detail}" if detail else _WARM_SPEC_SENTINEL


def _warm_spec_body(name: str, servers: dict[str, Any], description: str) -> dict[str, Any]:
    """A mint spec body, sentinel-stamped, plus the ``toolAliases`` its mounted set needs.

    THE writer chokepoint: every spec this module puts on disk comes through here, which is
    what lets :func:`_warm_spec_is_foreign` treat the sentinel as present-or-not-ours.
    """
    body = _mint_spec_body(name, servers, _warm_spec_description(description))
    aliases = connections_tool_aliases(list(servers))
    if aliases:
        body["toolAliases"] = aliases
    return body


def _registry_server_entry(provider: Provider) -> dict[str, Any] | None:
    """The remote MCP entry the registry implies for ``provider``, in wire shape."""
    entry: dict[str, Any] = {"url": provider["mcp_url"]}
    scopes = provider.get("recommended_scopes") or []
    if scopes:
        entry["scopes"] = list(scopes)
    client_id = provider.get("client_id")
    if client_id:
        entry["clientId"] = client_id
    # store_entry=None: registry-derived, so no store owns it.
    return kiro_oauth_wire_entry(entry, store_entry=None, server=str(provider["slug"]))


def _disabled_provider_slugs() -> set[str]:
    """Registry slugs whose configured MCP entry the user turned OFF."""
    disabled = {server.name for server in list_servers() if server.disabled}
    return {
        provider["slug"]
        for provider in get_visible_providers()
        if provider["slug"] in disabled or mcp_server_alias(provider["slug"]) in disabled
    }


def warm_spec_providers() -> list[Provider]:
    """The spec UNIVERSE: every provider the shared process ENUMERATES at spawn."""
    disabled = _disabled_provider_slugs()
    return [
        provider
        for provider in get_visible_providers()
        if provider["slug"] not in disabled and _warm_mintable_entry(provider, None) is not None
    ]


def _warm_activation_candidates(universe: list[Provider]) -> list[Provider]:
    """The subset of ``universe`` an activation should actually ask a URL for."""
    return [provider for provider in universe if not grant_present(provider["mcp_url"])]


def _warm_candidate_scan() -> tuple[list[Provider], list[Provider]]:
    """``(spec universe, activation candidates)`` from one pass over the registry."""
    try:
        universe = warm_spec_providers()
    except Exception:  # noqa: BLE001 — reads user config; degrade to warming nothing
        logger.debug("warm mint inventory read failed", exc_info=True)
        return [], []
    return universe, _warm_activation_candidates(universe)


def mintable_providers() -> list[Provider]:
    """Providers an activation should warm right now, registry order."""
    return _warm_candidate_scan()[1]


def _wanted_aliases(providers: list[Provider]) -> frozenset[str]:
    """The server aliases an activation must produce a challenge for."""
    return frozenset(mcp_server_alias(provider["slug"]) for provider in providers)


def _auth_shape(entry: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
    """The fields of an MCP entry that decide what an authorization asks for."""
    return (
        str(entry.get("url") or ""),
        tuple(kiro_entry_scopes(entry)),
        kiro_entry_client_id(entry),
    )


def _warm_mintable_entry(
    provider: Provider, configured: dict[str, Any] | None
) -> dict[str, Any] | None:
    """The REGISTRY entry the warm process would activate, or None if it cannot.

    Registry-derived on purpose: a plan built from the user's config changed on every
    Connect click, respawning a process holding other cards' live listeners.

    None in two cases: no usable auth configuration (no DCR and no pre-registered public
    client id -- GitHub is the standing example), or a CONFIGURED entry asking for
    something different from the registry, which only the cold path can honour without
    handing back a grant the user did not ask for.
    """
    entry = _registry_server_entry(provider)
    if entry is None:
        return None
    expectations: dict[str, Any] = dict(provider.get("l0_expectations") or {})
    # Through the accessor: the wire shape nests the client id under ``oauth``, so a
    # bare ``clientId`` lookup reads every registered non-DCR provider as unregistered.
    if not bool(expectations.get("dcr")) and not kiro_entry_client_id(entry):
        return None
    if isinstance(configured, dict) and _auth_shape(configured) != _auth_shape(entry):
        return None
    return entry


@dataclass(frozen=True)
class _WarmSpecPlan:
    """Every agent spec the warm process needs, plus a digest of their contents."""

    all_agent: str
    per_provider: dict[str, str]
    specs: dict[str, dict[str, Any]]
    entries: dict[str, dict[str, Any]]
    digest: str


def _plan_is_servable(resident: _WarmSpecPlan, wanted: _WarmSpecPlan) -> bool:
    """True when the RUNNING process's specs can still serve ``wanted``.

    Digest equality is the wrong test alone: it reads a set that SHRANK as a set that
    changed. The only thing a respawn can fix is a server the process was never told
    about, so a plan whose every entry is already resident with an identical authorization
    ask is servable -- and replacing the process would strand its peers' listeners for
    nothing. A changed url/scopes/client id is genuine incompatibility: authorizing the
    resident ask would hand back the wrong grant.
    """
    if not resident.all_agent:
        return False
    return all(resident.entries.get(alias) == entry for alias, entry in wanted.entries.items())


def _warm_spec_plan(providers: list[Provider]) -> _WarmSpecPlan:
    """Build (but do not write) the warm process's spec set."""
    agents_dir = _agent.kiro_agents_dir_path()
    configured = _agent._load_json(agents_dir / AGENT_FILENAME).get("mcpServers") or {}
    entries: dict[str, dict[str, Any]] = {}
    per_provider: dict[str, str] = {}
    for provider in providers:
        alias = mcp_server_alias(provider["slug"])
        entry = _warm_mintable_entry(provider, configured.get(alias))
        if entry is None:
            continue
        entries[alias] = entry
        per_provider[provider["slug"]] = f"{_WARM_AGENT_PREFIX}{alias}"

    # The BASE spec carries zero servers on purpose: it is what the process spawns on, so
    # anything it declared would be initialized -- and challenged for -- before any mint.
    specs: dict[str, dict[str, Any]] = {
        _WARM_BASE_AGENT: _warm_spec_body(
            _WARM_BASE_AGENT, {}, "Zero-server base spec for the shared approval-URL mint."
        )
    }
    if entries:
        specs[_WARM_ALL_AGENT] = _warm_spec_body(
            _WARM_ALL_AGENT, entries, "Every mintable provider: one activation warms every card."
        )
    for slug, name in per_provider.items():
        alias = mcp_server_alias(slug)
        specs[name] = _warm_spec_body(
            name, {alias: entries[alias]}, f"Single-provider approval-URL mint for {alias}."
        )
    digest = hashlib.sha256(
        json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _WarmSpecPlan(
        all_agent=_WARM_ALL_AGENT if entries else "",
        per_provider=per_provider,
        specs=specs,
        entries=entries,
        digest=digest,
    )


def _is_stale_warm_spec(stem: str, plan_names: frozenset[str]) -> bool:
    """Whether ``stem`` is a warm spec from a previous plan, safe to unlink.

    Three conjuncts, and the third is not redundant: a COLD mint spec for a server
    literally named ``warm-*`` shares this module's prefix
    (``kirocrew-mint-warm-foo-4821-9ab3c1de``), so prefix plus not-in-plan alone would
    delete a live cold mint's spec and strand a user mid-consent. Only the
    ``-<pid>-<8hex>`` suffix separates the two, over one shared character class.

    Necessary but NOT sufficient to unlink: the name says where a spec of ours would go,
    and :func:`_warm_spec_is_foreign` is what says the file there is one.
    """
    return (
        stem not in plan_names
        and _WARM_NAME_RE.match(stem) is not None
        and _MINT_NAME_RE.match(f"{stem}.json") is None
    )


def _warm_ownership_marks(name: str) -> dict[str, Any]:
    """The fields a warm spec named ``name`` carries no matter which plan wrote it.

    Read off the WRITER rather than restated, so a change to the spec body cannot leave
    this module unable to recognise its own files -- which would turn every rewrite into a
    refusal and hand the process a stale spec. ``mcpServers`` and ``tools`` vary with the
    plan, so neither can carry ownership.

    NECESSARY BUT NOT SUFFICIENT: every value here is a stock default, so a user's own spec
    plausibly matches all four. ``description`` is what discriminates -- not because its
    detail is fixed (it is not) but because its PREFIX is :data:`_WARM_SPEC_SENTINEL`, which
    :func:`_warm_spec_is_foreign` requires on top of these marks.
    """
    probe = _mint_spec_body(name, {}, "")
    return {key: probe[key] for key in ("model", "includeMcpJson", "prompt", "allowedTools")}


def _warm_spec_is_foreign(path: Path) -> bool:
    """True when ``path`` exists but no warm plan wrote it, so this module must not touch it.

    Warm spec names are FIXED and predictable and they sit in the user's OWN agents
    directory, so the name shape :func:`_is_stale_warm_spec` checks says where a spec of
    ours would GO -- never that the file already there is one. Ownership is proved from the
    contents instead, and it takes BOTH halves: the declared ``name`` matches the file with
    every field the writer fixes still holding, AND the description carries the sentinel.
    The marks alone are generic defaults, so a wholly user-authored spec that happens to
    carry them was read as ours and clobbered; the sentinel is the half that discriminates.

    Fails closed, because the two mistakes are not symmetric. Reading a file of ours as
    foreign costs one stale spec left as clutter; reading a user's hand-written agent as
    ours deletes it, or overwrites it with a spec they never asked for. So a file that is
    unreadable, not a JSON object, or shaped like anything but our own is foreign.
    """
    body = _agent._load_json(path)
    if not body:
        # ``_load_json`` answers {} for absent, unreadable and non-object alike, and only
        # the absent one is a path we may write -- which is what the presence check separates.
        return path.exists()
    marks = _warm_ownership_marks(path.stem)
    if body.get("name") != path.stem or any(body.get(key) != value for key, value in marks.items()):
        return True
    # No legacy exposure: no warm-spec writer has ever shipped, so no unsentinelled file of
    # ours exists anywhere to be orphaned by requiring this. Were one to exist it would read
    # as foreign, which means refused and left in place -- the safe direction.
    return not str(body.get("description") or "").startswith(_WARM_SPEC_SENTINEL)


def _write_warm_mint_specs(plan: _WarmSpecPlan) -> None:
    """Write the whole spec set, removing warm specs no longer in it.

    Every unlink and every write is gated on ownership, so a file this module did not write
    survives both. A refusal is audited and skipped, never raised: a provider whose spec
    path is occupied is a provider that goes unwarmed, not a failed spawn.
    """
    agents_dir = _agent.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    plan_names = frozenset(plan.specs)
    try:
        for path in agents_dir.glob(f"{_WARM_AGENT_PREFIX}*.json"):
            if not _is_stale_warm_spec(path.stem, plan_names):
                continue
            if _warm_spec_is_foreign(path):
                _log_warm_event("warm_mint_spec_sweep", path.name, outcome="refused")
                continue
            path.unlink(missing_ok=True)
    except OSError:
        logger.debug("warm mint spec sweep failed", exc_info=True)
    for name, spec in plan.specs.items():
        path = agents_dir / f"{name}.json"
        if _warm_spec_is_foreign(path):
            _log_warm_event("warm_mint_spec_write", path.name, outcome="refused")
            continue
        _agent._atomic_json_write(path, spec)


def _unowned_plan_specs(plan: _WarmSpecPlan) -> list[str]:
    """The planned spec names whose file is missing, or is not one this module wrote.

    Activation happens BY NAME: the runtime is handed ``agent=<fixed name>`` and kiro-cli
    resolves that name off this same agents directory. So the write's refusal protects the
    FILE and nothing else -- a hand-written agent sitting at a name we declined to
    overwrite would be spawned, and its ``mcpServers`` commands would initialize. This is
    what protects the SPAWN.

    Existence is tested as well as ownership, because :func:`_warm_spec_is_foreign` answers
    False for an absent path: a spec that is not there is equally not ours to activate.
    """
    agents_dir = _agent.kiro_agents_dir_path()
    unowned: list[str] = []
    for name in plan.specs:
        path = agents_dir / f"{name}.json"
        if not path.is_file() or _warm_spec_is_foreign(path):
            unowned.append(name)
    return unowned


def _remove_warm_mint_specs() -> None:
    """Unlink every warm spec THIS module wrote. Called when the process is retired."""
    try:
        for path in _agent.kiro_agents_dir_path().glob(f"{_WARM_AGENT_PREFIX}*.json"):
            if not _is_stale_warm_spec(path.stem, frozenset()):
                continue
            if _warm_spec_is_foreign(path):
                _log_warm_event("warm_mint_spec_removal", path.name, outcome="refused")
                continue
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — spec files; the write-time sweep catches leftovers
        logger.debug("warm mint spec removal failed", exc_info=True)


def _warm_work_dir() -> Path:
    """The shared process's working directory."""
    return data_home() / "connections" / "warm-mint"


def _runtime_alive(runtime: Any) -> bool:
    """Liveness of one warm process. Never raises into a mint."""
    if runtime is None:
        return False
    try:
        return bool(runtime.is_alive())
    except Exception:  # noqa: BLE001 — liveness must never raise into a mint
        logger.debug("warm mint liveness check failed", exc_info=True)
        return False


def _live_row_count(generation: int) -> int:
    """How many cards are still mid-consent on ``generation``."""
    return sum(
        1
        for entry in _mints.values()
        if entry.get("shared")
        and entry.get("generation") == generation
        and entry.get("state") in _LIVE_STATES
    )


def _generation_holds_live_rows(generation: int) -> bool:
    """True while killing ``generation`` would strand a redeemable code."""
    return _live_row_count(generation) > 0


def _activations_in_use() -> set[int]:
    """Activation ids a live shared row still points at -- the sweep's keep-set."""
    return {
        int(entry["activation"])
        for entry in _mints.values()
        if entry.get("shared") and entry.get("activation") and entry.get("state") in _LIVE_STATES
    }


@dataclass
class _WarmSession:
    """One live ACP session on the shared process, and what it owns.

    Held because the session owns the loopback callback servers for its challenges.
    ``settled`` flips once the URLs are absorbed into the mint table, which is what makes
    the sweep safe -- an activation still in flight is referenced by no row. ``expires_at``
    is why a session outlives the rows that pointed at it: a replaced URL may still be open
    on the provider's consent page, and one mint TTL is exactly the window in which that
    code is still redeemable.
    """

    generation: int
    handle: Any
    expires_at: float
    settled: bool = False


@dataclass(frozen=True)
class _WarmMintResult:
    """One activation's product, plus the snapshot and process it ran on."""

    generation: int
    activation: int
    providers: list[Provider]
    requests: list[dict[str, str]]


class _WarmMintRuntime:
    """One kiro-cli process shared by every card's approval-URL mint.

    Also the liveness registry a shared row's ``generation``/``activation`` are read
    against: the reader is what decides whether a card's URL is withdrawn, and the parked
    case is exactly the one where a wrong answer destroys a code the user could still
    redeem.
    """

    def __init__(self) -> None:
        self._runtime: Any = None
        self._plan: _WarmSpecPlan | None = None
        self._digest = ""
        #: Bumped on every spawn. Rows record the generation that minted them, letting a
        #: stand-down tell "nothing needs this" from "killing it strands a user mid-consent".
        self._generation = 0
        #: Generations kept alive ONLY because a card still holds one of their URLs.
        #: New mints never route here; the reaper kills each once its rows are gone.
        self._retiring: list[tuple[int, Any]] = []
        #: Live sessions by activation id -- each owns the loopback servers for its
        #: challenges, so one is held while a card points at one of its URLs.
        self._sessions: dict[int, _WarmSession] = {}
        self._activation_seq = 0
        self._lock = asyncio.Lock()
        self._reaper: Any = None

    def is_alive(self) -> bool:
        return _runtime_alive(self._runtime)

    def generation_is_live(self, generation: int) -> bool:
        """True while the process that minted ``generation`` can still redeem.

        A generation keeps its NUMBER while it is parked: only a successful spawn bumps the
        counter, so between a stand-down and its replacement the CURRENT number names a
        process that lives in ``_retiring`` with ``self._runtime`` already cleared. The
        equality test therefore CONFIRMS liveness but never denies it -- a miss falls
        through to the parked list. Answering False there withdrew redeemable URLs, and the
        withdrawal then made ``_generation_holds_live_rows`` false, so the next sweep killed
        the very process the park existed to preserve. Readers reach this without the
        runtime lock (the dashboard status path does), so the window is observable.
        """
        if generation <= 0:
            return False
        if generation == self._generation and self.is_alive():
            return True
        return any(
            parked == generation and _runtime_alive(runtime) for parked, runtime in self._retiring
        )

    def activation_is_live(self, activation: int) -> bool:
        """True while the SESSION that minted ``activation`` still listens."""
        if activation <= 0:
            return False
        return activation in self._sessions

    def parked_count(self) -> int:
        """How many generations are parked -- alive only for a card still mid-consent.

        Read by :func:`_drain_parked_generations`, which is what retires them once the
        current process is gone and no new mint will sweep them.
        """
        return len(self._retiring)

    async def settle_activation(self, activation: int, in_use: set[int]) -> None:
        """Mark ``activation`` absorbed, then collect the sessions nothing needs."""
        async with self._lock:
            record = self._sessions.get(activation)
            if record is not None:
                record.settled = True
            await self._sweep_sessions_locked(in_use)

    async def sweep_sessions(self, in_use: set[int]) -> None:
        """Collect settled sessions no live row points at."""
        async with self._lock:
            await self._sweep_sessions_locked(in_use)

    async def _sweep_sessions_locked(self, in_use: set[int]) -> None:
        """Collect settled sessions no row needs AND whose TTL has run out."""
        now = time.monotonic()
        doomed = [
            activation
            for activation, record in self._sessions.items()
            if record.settled and activation not in in_use and record.expires_at <= now
        ]
        for activation in doomed:
            record = self._sessions.get(activation)
            if record is None:
                continue
            destroyed = False
            try:
                await _destroy_session_quietly(record.handle)
                destroyed = True
            finally:
                # Forgotten only once the destroy completed. The record is the only reference
                # left to the handle, so losing it mid-destroy leaves a session -- and the
                # loopback servers it owns -- with nothing that could ever retry. Held across
                # the await deliberately: a reader without the lock then over-reports the
                # session as live for a moment, which keeps a row waiting a beat longer
                # rather than withdrawing a URL, the safe direction.
                if destroyed:
                    self._sessions.pop(activation, None)

    def _drop_generation_sessions(self, generation: int) -> None:
        """Forget one generation's sessions. Its process death already reaped them."""
        for activation in [
            activation
            for activation, record in self._sessions.items()
            if record.generation == generation
        ]:
            self._sessions.pop(activation, None)

    async def mint_for(self, *, slug: str = "") -> _WarmMintResult | None:
        """Ensure a live process, activate it, return its challenges."""
        async with self._lock:
            await self._sweep_retiring_locked()
            universe, candidates = await asyncio.to_thread(_warm_candidate_scan)
            if slug and not any(provider["slug"] == slug for provider in candidates):
                return None
            # The UNIVERSE decides what the process must have enumerated; the CANDIDATES
            # decide what this activation asks for. Apart, a grant moves the second only.
            if not await self._ensure_locked(universe):
                return None
            plan = self._plan
            if plan is None:
                return None
            agent = plan.per_provider.get(slug, "") if slug else plan.all_agent
            if not agent:
                return None
            wanted = [
                provider
                for provider in candidates
                if provider["slug"] in plan.per_provider and (not slug or provider["slug"] == slug)
            ]
            if not wanted:
                return None
            generation = self._generation
            try:
                activation, requests = await self._activate_locked(agent, _wanted_aliases(wanted))
            except Exception as exc:
                if self.is_alive():
                    # One activation failed but the process still serves: its other verifiers
                    # are intact, so killing it here destroys still-completable consent flows.
                    raise
                await self._park_or_kill_locked()
                raise _WarmMintDied(type(exc).__name__) from exc
            return _WarmMintResult(
                generation=generation,
                activation=activation,
                providers=wanted,
                requests=requests,
            )

    async def _ensure_locked(self, providers: list[Provider]) -> bool:
        """Guarantee a process whose specs can serve ``providers``."""
        try:
            plan = await asyncio.to_thread(_warm_spec_plan, providers)
        except Exception:  # noqa: BLE001 — reads user-editable JSON; never fail a mint
            logger.debug("warm mint spec plan failed", exc_info=True)
            return False
        if not plan.all_agent:
            return False

        if self.is_alive() and self._digest == plan.digest:
            return True
        resident = self._plan
        if self.is_alive() and resident is not None and _plan_is_servable(resident, plan):
            logger.info(
                "Shared mint generation %d already serves the new candidate set; "
                "re-activating instead of respawning",
                self._generation,
            )
            return True
        if self._runtime is not None:
            logger.info(
                "Starting a new shared mint generation (%s)",
                "specs are incompatible" if self.is_alive() else "process is gone",
            )
        await self._park_or_kill_locked()

        runtime: Any = None
        handed_over = False
        try:
            await asyncio.to_thread(_write_warm_mint_specs, plan)
            # The write REFUSES a path it does not own rather than clobbering it, and the
            # spawn below activates by NAME -- so without this the refusal would hand
            # kiro-cli a stranger's spec to execute. Aborting is safe and honest: the cold
            # path still serves every Connect, it just spawns per provider.
            unowned = await asyncio.to_thread(_unowned_plan_specs, plan)
            if unowned:
                logger.warning(
                    "Not warming: %d planned mint spec(s) are not ours to activate", len(unowned)
                )
                await asyncio.to_thread(
                    _log_warm_event,
                    "connections_warm_mint_spawn",
                    f"unowned_specs:{len(unowned)}",
                    "refused",
                )
                return False
            runtime = _acp_runtime_factory()(
                work_dir=await asyncio.to_thread(_warm_work_dir),
                agent=_WARM_BASE_AGENT,
                sandbox_mode="auto",
            )
            await asyncio.wait_for(runtime.spawn(), timeout=_WARM_SPAWN_TIMEOUT_SECONDS)
            # No await between the spawn returning and the flag: the handover is a run of
            # plain assignments plus a synchronous ``create_task``, so there is no window
            # in which the process is ours but the ``finally`` would still tear it down.
            self._runtime, self._plan, self._digest = runtime, plan, plan.digest
            self._generation += 1
            self._reaper = asyncio.get_running_loop().create_task(
                _warm_mint_reaper(self._generation)
            )
            handed_over = True
        except Exception as exc:  # noqa: BLE001 — degrade to the cold path
            logger.warning("Shared mint process spawn failed: %s", type(exc).__name__)
            return False
        finally:
            # A ``finally``, not an ``except Exception``: the stand-down above has already
            # parked the old generation AND cancelled its reaper, so a CancelledError in the
            # spec write or the spawn would otherwise leave that process with nothing that
            # will ever sweep it -- the parked-generation leak by a third route -- plus a
            # forked child and a set of specs nobody owns.
            if not handed_over:
                await self._abandon_spawn_locked(runtime)
        await asyncio.to_thread(
            _log_warm_event,
            "connections_warm_mint_spawn",
            f"providers:{len(plan.per_provider)} generation:{self._generation}",
        )
        return True

    async def _abandon_spawn_locked(self, runtime: Any) -> None:
        """Undo a spawn attempt that never became this object's process.

        Reached from a ``finally``, so it covers cancellation as well as failure.
        """
        if self._retiring:
            # Armed BEFORE any await, because ``create_task`` is synchronous: the parked
            # generation then has its sweeper even if the teardown below is interrupted.
            # Stored as the reaper so a later spawn's own reaper replaces it.
            self._reaper = asyncio.get_running_loop().create_task(_drain_parked_generations())
        if runtime is not None:
            await _kill_quietly(runtime)
        if not self._retiring:
            await asyncio.to_thread(_remove_warm_mint_specs)

    async def _abandon_session_creation_locked(self, create: Any) -> None:
        """Reap a session the backend may have created after we stopped waiting for it.

        ``create`` was shielded from our own cancellation precisely so its result stays
        reachable here: the handle is the ONLY way to terminate the session and the loopback
        callback children it owns. A handle that does arrive is REGISTERED before it is
        destroyed -- settled and already expired -- so it enters the same ownership rule the
        rest of this class uses, and an interrupted destroy is retried by the ordinary sweep
        instead of being lost.
        """
        handle: Any = None
        try:
            # Bounded: this only buys back a session already on its way.
            handle = await asyncio.wait_for(
                asyncio.shield(create), timeout=_WARM_SESSION_REAP_TIMEOUT_SECONDS
            )
        except BaseException:  # noqa: BLE001 — best-effort reap; the caller re-raises its own
            handle = None
        if handle is None:
            # Nothing addressable exists to destroy: the backend may still accept a session
            # carrying an id we never received. QUARANTINE the generation rather than trust
            # retirement to arrive on its own -- it is not guaranteed to. Any card holding a
            # URL keeps ``_shared_mints_pending`` true, which resets the reaper's idle clock
            # every cycle, while ``_ensure_locked``'s digest fast path keeps this same
            # process reusable; so without this, every repetition of this path parked another
            # unaddressable session and its callback children on ONE live runtime, without
            # bound. Clearing the resident plan makes the next activation find it unservable,
            # which stands this generation down through the ordinary path (parked for the
            # drain when a card still needs it, killed outright otherwise) and takes the
            # orphan with it. The residual is therefore at most ONE generation's sessions.
            #
            # Scoped deliberately to this branch: the recovered-handle path below repairs
            # itself completely, so quarantining there would cost a respawn on every
            # transient timeout for nothing.
            self._plan, self._digest = None, ""
            logger.warning(
                "Quarantining shared mint generation %d: a session was created that we "
                "cannot address, so the process is stood down at the next activation",
                self._generation,
            )
            create.cancel()
            return
        self._activation_seq += 1
        activation = self._activation_seq
        self._sessions[activation] = _WarmSession(
            generation=self._generation, handle=handle, expires_at=0.0, settled=True
        )
        destroyed = False
        try:
            await _destroy_session_quietly(handle)
            destroyed = True
        finally:
            if destroyed:
                self._sessions.pop(activation, None)

    async def _activate_locked(
        self, agent: str, wanted: frozenset[str]
    ) -> tuple[int, list[dict[str, str]]]:
        """Activate ``agent`` on the shared process and return its challenges."""
        runtime = self._runtime
        if runtime is None or not self.is_alive():
            raise _WarmMintUnsafe("the shared mint process is not alive")
        servers = _warm_session_mcp_servers()
        if servers:
            # Never reachable from our own code -- the guard exists because the failure is
            # silent and total: session/new-injected servers kill the process and its verifiers.
            raise _WarmMintUnsafe("session-injected MCP servers would kill the shared process")

        # OWNERSHIP TRANSFER, and the one that has no second chance: the returned handle is
        # the ONLY way to terminate a session and the loopback callback children it owns, so
        # a wait we abandon after the backend already accepted `session/new` would leak them
        # until the whole runtime is retired. The create runs as a task we keep a reference
        # to and shield, so the handle stays REACHABLE even when we stop waiting for it.
        create = asyncio.get_running_loop().create_task(
            runtime.create_session(agent=agent, mcp_servers=servers)
        )
        try:
            handle = await asyncio.wait_for(
                asyncio.shield(create), timeout=_WARM_SESSION_TIMEOUT_SECONDS
            )
        except BaseException:
            await self._abandon_session_creation_locked(create)
            raise
        # No await between the handle arriving and its registration, so there is no window
        # in which the session exists and nothing in this object knows about it.
        self._activation_seq += 1
        activation = self._activation_seq
        self._sessions[activation] = _WarmSession(
            generation=self._generation,
            handle=handle,
            expires_at=time.monotonic() + _MINT_TTL_SECONDS,
        )
        collected: dict[str, dict[str, str]] = {}
        try:
            for round_index in range(_WARM_OAUTH_SETTLE_ROUNDS):
                for request in handle.pop_pending_oauth_requests():
                    name = str(request.get("serverName") or "")
                    if name and request.get("oauthUrl"):
                        collected[name] = request
                if wanted and wanted <= collected.keys():
                    break
                if round_index + 1 < _WARM_OAUTH_SETTLE_ROUNDS:
                    # CONSUME the queue rather than sleep past it. This is the whole
                    # mechanism: ``pop_pending_oauth_requests`` reads a list that only
                    # ``drain_init`` appends to, and ``create_session`` runs exactly one
                    # drain before handing the handle over -- so a bare sleep here moved
                    # nothing, and a frame arriving after that drain's idle exit was
                    # unreachable however many rounds elapsed. The budget was never the
                    # binding constraint; the loop had no way to absorb a late frame at all.
                    #
                    # ``no_report_ceiling=0.0`` is load-bearing: it arms the idle shortcut at
                    # entry, so this call cannot hold waiting for a "first report" that this
                    # session already produced during ``create_session``'s own drain. That is
                    # precisely the idle-window semantics that made an unbounded drain the
                    # wrong tool here -- bounded per round, it is the right one. Each round is
                    # therefore a window of at most ``_WARM_OAUTH_SETTLE_SECONDS`` that
                    # returns as soon as the queue goes quiet, so the total budget is
                    # unchanged and a satisfied activation still short-circuits on the pop
                    # above without opening a window at all.
                    await handle.drain_init(
                        duration=_WARM_OAUTH_SETTLE_SECONDS,
                        idle_exit=_WARM_OAUTH_SETTLE_SECONDS,
                        no_report_ceiling=0.0,
                    )
        except BaseException:
            # Nothing will ever be stamped with this activation, so the session it
            # registered would leak past the sweep's settled-only rule. Marked settled and
            # already expired FIRST, so that if the destroy below is interrupted the sweep
            # is a real retry; and popped only once the destroy actually completed, because
            # the record is the only reference left to the handle.
            record = self._sessions.get(activation)
            if record is not None:
                record.settled = True
                record.expires_at = 0.0
            await _destroy_session_quietly(handle)
            self._sessions.pop(activation, None)
            raise
        return activation, list(collected.values())

    async def shutdown(self) -> None:
        async with self._lock:
            await self._retire_locked()

    async def sweep_retiring(self) -> None:
        """Kill parked generations nothing is waiting on any more."""
        async with self._lock:
            await self._sweep_retiring_locked()

    async def _sweep_retiring_locked(self) -> None:
        keep: list[tuple[int, Any]] = []
        drop: list[tuple[int, Any]] = []
        for pair in self._retiring:
            generation, runtime = pair
            needed = _runtime_alive(runtime) and _generation_holds_live_rows(generation)
            (keep if needed else drop).append(pair)
        self._retiring = keep
        try:
            while drop:
                generation, runtime = drop[0]
                logger.info("Retiring parked shared mint generation %d", generation)
                await self._kill_generation(generation, runtime)
                # Popped only once its kill has actually completed.
                drop.pop(0)
        finally:
            if drop:
                # Whatever this sweep did not finish stays PARKED. Removing it from the list
                # without killing it would make ``parked_count()`` read zero, so the drain
                # would exit and no later sweep would retry -- the process, its sessions and
                # their loopback servers would simply leak.
                self._retiring = drop + self._retiring

    async def _park_or_kill_locked(self) -> None:
        """Stand the current process down: PARKED when a card still needs it."""
        runtime, generation = self._runtime, self._generation
        reaper, self._reaper = self._reaper, None
        self._runtime, self._plan, self._digest = None, None, ""
        # The reaper is the caller on the idle path; cancelling the current task
        # would abandon the kill it is in the middle of awaiting.
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
        if runtime is None:
            return
        if _runtime_alive(runtime) and _generation_holds_live_rows(generation):
            self._retiring.append((generation, runtime))
            logger.info(
                "Parking shared mint generation %d: %d card(s) still mid-consent on it",
                generation,
                _live_row_count(generation),
            )
            return
        try:
            await self._kill_generation(generation, runtime)
        except BaseException:
            # ``_runtime`` and the reaper were cleared synchronously above, so this list is
            # now the ONLY reference to a process the kill did not finish with. Park it and
            # arm the drain rather than dropping it: an untracked child never gets retired.
            self._retiring.append((generation, runtime))
            if self._reaper is None:
                self._reaper = asyncio.get_running_loop().create_task(_drain_parked_generations())
            raise

    async def _kill_generation(self, generation: int, runtime: Any) -> None:
        """Kill one process and expire the links only it could have redeemed."""
        await _kill_quietly(runtime)
        self._drop_generation_sessions(generation)
        await _expire_shared_mints("mint_process_gone", generation=generation)
        if self._runtime is None and not self._retiring:
            await asyncio.to_thread(_remove_warm_mint_specs)

    async def _retire_locked(self) -> None:
        """Hard teardown: every generation, parked ones included."""
        reaper, runtime, generation = self._reaper, self._runtime, self._generation
        # Parked generations first, then the current one -- a parked process is the one a
        # card may still be mid-consent on, so it is retired before the live one.
        pending = list(self._retiring)
        if runtime is not None:
            pending.append((generation, runtime))
        had_work = bool(pending)
        self._retiring = []
        self._reaper = self._runtime = None
        self._plan, self._digest = None, ""
        self._sessions.clear()
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
        try:
            while pending:
                doomed_generation, doomed_runtime = pending[0]
                await _kill_quietly(doomed_runtime)
                await _expire_shared_mints("mint_process_gone", generation=doomed_generation)
                # Popped only once this generation is fully retired.
                pending.pop(0)
        finally:
            if pending:
                # Same rule as everywhere else in this class: a process whose teardown did
                # not complete stays tracked, with something scheduled to retire it. The
                # lists above were emptied synchronously, so this is the only reference left.
                self._retiring = pending
                self._reaper = asyncio.get_running_loop().create_task(_drain_parked_generations())
        if had_work and not self._retiring:
            # Only once nothing is left: a spec removed under a still-running parked process
            # strands it without the file it was spawned on.
            await asyncio.to_thread(_remove_warm_mint_specs)


async def _destroy_session_quietly(handle: Any) -> None:
    """Terminate one warm session. Only ever called once nothing points at it."""
    try:
        await asyncio.wait_for(handle.destroy(), timeout=_WARM_SESSION_DESTROY_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — best-effort teardown of our own session
        logger.debug("warm mint session destroy failed", exc_info=True)


async def _kill_quietly(runtime: Any) -> None:
    try:
        await asyncio.wait_for(runtime.kill(), timeout=_WARM_KILL_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — best-effort teardown of our own child
        logger.debug("warm mint runtime kill failed", exc_info=True)


_warm_mint = _WarmMintRuntime()


def _warm_row_alive(entry: MintState) -> bool:
    """Whether a SHARED row's URL can still actually be redeemed.

    Two things must be alive and they die independently: the PKCE verifier in the PROCESS,
    and the loopback listener in the SESSION. Process liveness alone passed a
    terminated-session row, which is how a card kept serving an unredeemable URL -- which
    is also why the cold engine's ``_mint_holder_alive`` is deliberately NOT reused: it
    reads the row's own ``client``, which a shared row does not own.
    """
    if not _warm_mint.generation_is_live(int(entry.get("generation") or 0)):
        return False
    return _warm_mint.activation_is_live(int(entry.get("activation") or 0))


def _shared_mints_pending() -> bool:
    """True while any card still needs the shared process alive."""
    return any(
        entry.get("shared") and entry.get("state") in _LIVE_STATES for entry in _mints.values()
    )


async def _expire_shared_mints(reason: str, *, generation: int | None = None) -> list[str]:
    """Flip live shared mints stale. Called when a process is gone.

    ``generation`` is the only narrowing there is, and every caller passes it: the rows a
    dead process can no longer redeem are exactly the ones it minted. A pass narrowed by
    the CALLER's own row tokens instead used to exist here; it read as "spare my retry" but
    meant "expire every other generation", which withdrew a parked generation's redeemable
    URL. Withdrawal follows the verifier, so it follows the generation.
    """
    flipped: list[str] = []
    async with _mints_lock:
        for slug, entry in _mints.items():
            if not entry.get("shared") or entry.get("state") not in _LIVE_STATES:
                continue
            if generation is not None and entry.get("generation") != generation:
                continue
            entry["state"] = "expired"
            entry["reason"] = reason
            await _dispose_mint(entry)
            flipped.append(slug)
    if flipped:
        logger.info("Shared mint process gone; %d pending mint(s) flipped stale", len(flipped))
    return flipped


async def expire_dead_mints() -> list[str]:
    """Withdraw every shared row whose holding process is gone. THE chokepoint."""
    doomed: list[str] = []
    async with _mints_lock:
        for slug, entry in _mints.items():
            if not entry.get("shared") or entry.get("state") != "waiting":
                continue
            if _warm_row_alive(entry):
                continue
            entry["state"] = "expired"
            entry["reason"] = "mint_process_gone"
            await _dispose_mint(entry)
            doomed.append(slug)
    if doomed:
        logger.info("Withdrew %d approval URL(s) whose minting process is gone", len(doomed))
    return doomed


async def _warm_activate(*, slug: str = "") -> _WarmMintResult | None:
    """Activate the shared process, surviving one death of it.

    The death path does NOT expire anything itself. The stand-down inside ``mint_for`` --
    reached only when the process is already gone -- has just run
    ``_expire_shared_mints(generation=<the dead one>)`` through ``_kill_generation``, so the
    rows whose verifier died are already withdrawn, scoped to that generation. A second,
    unscoped pass here withdrew every other live shared row instead: a PARKED generation's
    URL is still redeemable (its process holds the verifier and its session still answers
    the redirect) and a concurrent batch's claims are another activation's to fill.

    ``CancelledError`` is deliberately NOT caught: swallowing it would report a successful
    stand-down to a caller that is being torn down. The caller releases its claims and
    re-raises (see ``warm_mint_all``).
    """
    for attempt in range(_WARM_ACTIVATION_ATTEMPTS):
        try:
            return await _warm_mint.mint_for(slug=slug)
        except _WarmMintDied as died:
            last = attempt + 1 >= _WARM_ACTIVATION_ATTEMPTS
            logger.warning(
                "Shared mint process died mid-activation (%s); %s",
                died.cause,
                "falling back to the cold path" if last else "respawning it",
            )
        except Exception as exc:  # noqa: BLE001 — the process lives; degrade to cold
            logger.warning(
                "Shared mint activation failed on a live process (%s); "
                "falling back to the cold path",
                type(exc).__name__,
            )
            return None
    return None


async def _drain_parked_generations() -> None:
    """Keep sweeping until no parked generation is left, then return.

    A parked generation is a process kept alive ONLY because a card still holds one of its
    URLs, so it can be retired the moment that card grants, cancels or times out -- and
    ``sweep_retiring`` is the only thing that retires it. Every other route to that sweep
    runs off a NEW mint (``mint_for``, or the reaper a fresh spawn creates), which is
    precisely what the leak does not have: once the current process is gone and no further
    Connect arrives, nothing calls it again and the parked process, its sessions and their
    loopback servers stay resident indefinitely.

    Returns immediately when nothing is parked, so the callers can invoke it unconditionally.
    """
    while _warm_mint.parked_count():
        await asyncio.sleep(_MINT_GRANT_POLL_SECONDS)
        await _warm_mint.sweep_retiring()
        # A parked process can also die while parked, which withdraws its cards' URLs.
        await expire_dead_mints()
        async with _mints_lock:
            in_use = _activations_in_use()
        await _warm_mint.sweep_sessions(in_use)


async def _warm_mint_reaper(generation: int) -> None:
    """Retire the shared process once no card is waiting on it.

    Outlives its own generation on the death path: standing the current process down does
    not release the generations parked behind it, so the reaper drains those before it
    returns (see :func:`_drain_parked_generations`).
    """
    idle_since = 0.0
    try:
        while True:
            await asyncio.sleep(_MINT_GRANT_POLL_SECONDS)
            await _warm_mint.sweep_retiring()
            # Every generation, parked ones included: a card pointing at a process that is
            # gone must not keep its URL until this reaper's own generation is the dead one.
            await expire_dead_mints()
            # Sessions outlive the rows that needed them on every path ending a mint without
            # a new activation (grant, cancel, TTL). Collected here, not for the process life.
            async with _mints_lock:
                in_use = _activations_in_use()
            await _warm_mint.sweep_sessions(in_use)
            if not _warm_mint.is_alive():
                await _expire_shared_mints("mint_process_gone", generation=generation)
                await _drain_parked_generations()
                return
            if _shared_mints_pending():
                idle_since = 0.0
                continue
            now = time.monotonic()
            if idle_since == 0.0:
                idle_since = now
            elif now - idle_since >= _WARM_IDLE_GRACE_SECONDS:
                logger.info("Retiring the idle shared mint process")
                # Hard teardown, parked generations included -- so no drain is needed after.
                await _warm_mint.shutdown()
                return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — the reaper must never take the gateway down
        logger.debug("warm mint reaper failed", exc_info=True)


def _mint_is_cold_held(entry: MintState | None) -> bool:
    """True when a dedicated client -- not the shared process -- holds this URL."""
    return entry is not None and entry.get("state") == "waiting" and entry.get("client") is not None


async def _claim_shared_mints(slugs: list[str]) -> tuple[dict[str, str], list[MintState]]:
    """Claim ``slugs`` for the shared process. Returns ``({slug: row token}, displaced rows)``.

    The token is the row's OWN identity and it is what every later step fences on. A batch
    ``time.monotonic()`` reading cannot do that job: it has ~15.6ms granularity on Windows,
    so two Connects for one provider inside a single tick read as the same row and a late
    absorb writes its URL over the newer claim (see ``_new_mint_token``, which records the
    same reasoning for the cold engine).

    ATOMIC BY CONSTRUCTION: the loop contains NO await, so the caller either gets every
    claim or none. It used to await ``_dispose_mint`` on each replaced row, which suspends
    on a client teardown and again on the shielded spec removal in that function's
    ``finally`` -- and the claim is taken BEFORE ``warm_mint_all`` enters the try that rolls
    it back, so a cancellation there left earlier slugs installed as ``minting`` with no
    caller holding their tokens. Nothing withdraws such a row (``expire_dead_mints`` judges
    ``waiting`` only) and it keeps ``_shared_mints_pending`` true, so the process is never
    retired either. The replaced rows come back for the caller to dispose INSIDE that try
    instead -- which also puts the dispose outside the table lock, where the mint engine's
    own rule wants it.
    """
    claimed: dict[str, str] = {}
    displaced: list[MintState] = []
    started = time.monotonic()
    async with _mints_lock:
        for slug in slugs:
            prior = _mints.get(slug)
            if _mint_is_cold_held(prior):
                # A dedicated client owns this provider's verifier. Leave its URL on
                # the card rather than replace a working link.
                continue
            if prior is not None:
                # Hand it back, don't just drop it: the replaced row may own a watcher, and
                # a watcher outliving its row expires the NEW mint on the OLD mint's
                # deadline. Recorded only alongside the claim that displaced it, so a
                # non-empty list always implies a non-empty claim set.
                displaced.append(prior)
            token = _new_mint_token()
            _mints[slug] = {
                "state": "minting",
                # Informational only -- when the claim was taken. Never a fence.
                "started": started,
                "shared": True,
                "token": token,
            }
            claimed[slug] = token
    return claimed, displaced


async def _dispose_displaced_rows(rows: list[MintState]) -> None:
    """Release the holdings of the rows a claim replaced -- watcher, client, PID, spec.

    Split out of the claim itself because it awaits: see ``_claim_shared_mints``. Called
    from inside the caller's protected region, so a cancellation here rolls the claims back
    rather than stranding them.
    """
    for row in rows:
        await _dispose_mint(row)


async def _release_shared_claims(claims: dict[str, str]) -> None:
    """Drop unfulfilled claims so the card asks for a fresh mint.

    Keyed on the row token, so a claim already superseded by a newer one at the same slug
    is left alone rather than dropped out from under the activation now filling it.
    """
    async with _mints_lock:
        for slug, token in claims.items():
            entry = _mints.get(slug)
            if entry is not None and entry.get("token") == token and entry.get("shared"):
                await _dispose_mint(entry)
                _mints.pop(slug, None)


def _credential_bearing_slugs(urls: dict[str, str]) -> set[str]:
    """The slugs in ``{slug: url}`` whose approval URL carries a credential.

    The gate is :func:`~kiro_crew.security.oauth_url_contains_credential` -- the same
    predicate the cold mint and the chat consent banner apply -- and it is synchronous
    because it can consult the operator's on-disk OAuth-endpoint extension, an unbounded
    stat on a network mount. Never returns or logs the value it judged.
    """
    return {slug for slug, url in urls.items() if url and oauth_url_contains_credential(url)}


async def _absorb_warm_requests(result: _WarmMintResult, claims: dict[str, str]) -> list[str]:
    """Move popped challenges into the mint table. Returns the slugs now waiting."""
    popped = {
        str(request.get("serverName") or ""): str(request.get("oauthUrl") or "")
        for request in result.requests
    }
    # An activation names its challenges by the alias the SPEC mounted; the table is keyed by
    # registry slug. Resolved once, here, so the screen and the store judge the same string.
    resolved = {
        slug: popped.get(mcp_server_alias(slug)) or popped.get(slug) or ""
        for slug in (provider["slug"] for provider in result.providers)
    }
    minted: list[str] = []
    unfulfilled: dict[str, str] = {}
    tainted: set[str] = set()
    try:
        # Screened BEFORE the table lock: the verdict is a pure function of the URL, and the
        # gate can read the operator's endpoint extension off disk. A refused URL never
        # reaches the store, so no card can be handed one.
        tainted = await asyncio.to_thread(_credential_bearing_slugs, resolved)
        loop = asyncio.get_running_loop()
        async with _mints_lock:
            for provider in result.providers:
                slug = provider["slug"]
                token = claims.get(slug, "")
                entry = _mints.get(slug)
                if entry is None or not token or entry.get("token") != token:
                    # Superseded while the activation ran. Its challenge stays alive in the
                    # session that produced it and nothing points at it; the sweep collects
                    # that session.
                    continue
                url = resolved.get(slug, "")
                if not url or slug in tainted:
                    # Released either way, not failed: the card asks for a fresh mint rather
                    # than sitting on a claim nothing will ever fill.
                    unfulfilled[slug] = token
                    continue
                entry.update(
                    {
                        "state": "waiting",
                        "oauth_url": url,
                        "generation": result.generation,
                        "activation": result.activation,
                        "watcher": loop.create_task(
                            _mint_watcher(slug, provider["mcp_url"], token)
                        ),
                    }
                )
                minted.append(slug)
    finally:
        # Settlement on EVERY post-activation exit, which is why it is a ``finally``. The
        # session was registered by ``_activate_locked`` before this function ran, and the
        # sweep only ever collects a SETTLED session -- so a raise anywhere above (the
        # screen's thread hop is a cancellation point, and the gate itself can fail on an
        # unreadable file) would leave the session, and the loopback callback servers it
        # owns, resident for the life of the process. The in-use snapshot is taken HERE,
        # under the lock, so it reflects whatever the loop above actually installed rather
        # than what it intended to.
        async with _mints_lock:
            in_use = _activations_in_use()
        await _warm_mint.settle_activation(result.activation, in_use)
    for slug in tainted:
        logger.warning("Shared mint for %r produced a URL with a credential pattern", slug)
        await asyncio.to_thread(
            _log_warm_event, "connections_warm_mint_url", f"provider:{slug}", "refused"
        )
    if unfulfilled:
        await _release_shared_claims(unfulfilled)
    return minted


async def warm_mint_all(providers: list[Provider] | None = None) -> list[str]:
    """Warm every mintable provider's approval URL in ONE activation.

    Returns the slugs now holding a URL. Never raises for a mint failure -- that leaves the
    cards exactly as they were, asking for a mint. CANCELLATION does propagate, after the
    claims are released.

    The claim is taken BEFORE the process is ensured, because ensuring and activating are
    one locked step (see ``mint_for``) and nothing may run between them. ``providers``
    therefore only decides which rows are CLAIMED; what gets activated is the snapshot the
    lock holder computes, so a claim the activation did not cover is released rather than
    left minting forever.

    ``POST /api/connections/premint`` passes the candidates it scanned, so an explicit
    ``providers`` is the ordinary call shape; ``None`` re-scans for a caller that has no
    list of its own.
    """
    candidates = (
        await asyncio.to_thread(mintable_providers) if providers is None else list(providers)
    )
    if not candidates:
        return []

    claims, displaced = await _claim_shared_mints([provider["slug"] for provider in candidates])
    if not claims:
        # A displaced row is only ever recorded alongside the claim that displaced it, so an
        # empty claim set means there is nothing to dispose either.
        return []
    try:
        # Inside the protected region on purpose: this is the awaiting half of the claim, so
        # a cancellation here must roll the whole claim set back rather than strand it.
        await _dispose_displaced_rows(displaced)
        result = await _warm_activate()
        if result is None:
            await _release_shared_claims(claims)
            return []

        minted = await _absorb_warm_requests(result, claims)
        activated = {provider["slug"] for provider in result.providers}
        stranded = {slug: token for slug, token in claims.items() if slug not in activated}
        if stranded:
            await _release_shared_claims(stranded)
    except BaseException:
        # BaseException, not Exception: a CancelledError landing between the claim and the
        # activation would otherwise leave every row ``minting`` with no watcher and no
        # activation. ``expire_dead_mints`` judges ``waiting`` rows only, so nothing would
        # ever withdraw them -- and they keep ``_shared_mints_pending`` true, so the reaper
        # never retires the process either. The release awaits to completion: a task is
        # handed its cancellation once, so this cleanup runs before the raise resumes it.
        await _release_shared_claims(claims)
        raise
    await asyncio.to_thread(
        _log_warm_event, "connections_warm_mint", f"activated:{len(claims)} minted:{len(minted)}"
    )
    logger.info("Shared mint activation warmed %d of %d card(s)", len(minted), len(claims))
    return minted

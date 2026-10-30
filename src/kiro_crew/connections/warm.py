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
failures are recorded in ``docs/system-specs/modules/connections.md``.

Four rules are load-bearing and recorded in that same note: the SESSION is HELD (see
:func:`_warm_row_alive`), specs are enumerated ONCE at spawn -- which fixes the mode's
mounted server set for the life of the process, so a plan that merely SHRANK is servable but
NOT reusable and respawns, with a process still holding a consent PARKED rather than killed
(:func:`_plan_is_servable`,
:func:`_resident_roster_is_asked_for`, :meth:`_WarmMintRuntime._park_or_kill_locked`), the
spec universe is registry-derived and BLIND to grant and cancel state because a plan
tracking who needs a URL now retires a process holding other cards' listeners, and a warm
session injects an EMPTY ``mcp_servers`` list because remote servers passed through
``session/new`` kill the process with every pending verifier in it
(:func:`_warm_session_mcp_servers`).

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
``_retire_locked``, ``_abandon_spawn_locked``). Pinned by AST guards in
``test/test_connections_warm.py`` (every awaited window is protected) and
``test/test_connections_warm_activation.py`` (the claim loop holds no await), not merely
described here.

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

OWNERSHIP: mode names remain fixed, but their files live only in one random, owner-only
PROJECT scope under ``<data home>/run/connections-warm/generations``. That generation root is
the runtime cwd, so both the initial ``--agent`` and later ``session/set_mode`` resolve the
private ``.kiro/agents`` files before the user's global scope; the picker refuses the sensitive
run tree. Every spec is still stamped with :data:`_WARM_SPEC_SENTINEL`, because content proof
protects same-user races and licenses one-time cleanup of sentinel-owned global leftovers.
An unsentinelled global file is never auto-deleted: a name cannot prove ownership.

GENERATION LIFETIME follows the process. Its PID-reuse-resistant owner marker is written
before spawn and completed with the child identity after spawn. Parking or a failed kill keeps
the directory attached; a confirmed kill releases it. Gateway startup scavenges only roots
whose gateway and runtime identities are both provably dead, while graceful cleanup retires the
live singleton. Missing, malformed or unreadable evidence fails closed to retained clutter.

INVARIANT: no coroutine here or in the ``warm_runtime`` owners touches the filesystem
directly. The spec helpers read the user's config, private generation tree, global legacy agents
dir, or kiro-cli's OAuth cache, and the credential gate reads the operator's OAuth-endpoint
extension -- any of which can sit on a network mount where a stat is unbounded -- so they are
synchronous, and a coroutine reaches them through ``asyncio.to_thread``. Enforced by a
fixed-point drift guard in ``test/test_connections_warm_off_loop.py`` that reads this module
and its owners as one call graph, not merely described here.

REQUEST PATH: :func:`warm_mint_all` is driven by ``POST /api/connections/premint``, which the
Connections page fires once on mount. It scans the candidates and hands them over, so the slugs
it reports and the rows the engine claims come from one registry read. The consumer of what it
warms is :func:`adopt_shared_mint`, which ``POST /api/connections/mint`` tries BEFORE reserving
a row of its own -- without it that endpoint's ``reserve_mint_row`` popped the shared row and
disposed the very URL the click had been warmed for. A table miss re-drains the newest live
shared session that activated the provider and retries adoption before the dedicated cold path.

RESILIENCE is lifecycle-owned by the same reaper that detects process death. An explicit page
warm arms ONE automatic replacement. The dead generation reserves one single-flight re-arm task,
which reuses the ordinary tri-state candidate scan and ``warm_mint_all`` path without granting
itself another attempt. Concurrent observers share that task; deliberate shutdown disarms and
cancels it before retiring the process. No provider-level recovery state crosses the API.
Proactive refresh attaches in :func:`_warm_mint_reaper` when its dependent slice lands.

COMPOSITION: this module is the import and patch surface, and eight private owners live in
:mod:`kiro_crew.connections.warm_runtime` -- the desired plan and its reuse rules
(``spec_plan``), recorded-process start identity (``start_identity``), the two-axis
ownership of shared rows with the atomic claim and its rollback (``shared_rows``, which also
records the row IDENTITY, ATOMICITY and TWO AXES rules), the text and fixed names of a warm
spec (``spec_body``), the private generation tree and its owner marker (``generation_tree``),
the shared process's primitives -- its runtime class, liveness, session records and reporting
teardown (``mint_process``) -- the automatic re-arms (``rearms``), and the request path that
turns an activation into rows and hands one to a Connect (``table_flow``). Each is re-exported
here as the same object. An owner reads every name this module defines, and every name a test
or caller substitutes on it, through this module at call time -- never by its bare name -- so a
patch here reaches it. What stays here stays because a repository gate pins it to this file or
because it is the runtime those owners call into: the spec-ownership judge and the generation
screens (link-screen sites), the death verdict and the boot scavenger built on those screens
(the composition contract's pinned-construct list), the configured-spec read (the agent-spec
call-site ratchet), the runtime and its session table with the reaper and the drain that sweep
it (the session-registry gate), the credential screen the security spec cites, and the provider
scan, the gated spec writer and the expiry those call.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import stat
import time
from pathlib import Path
from typing import Any

from kiro_crew import agent as _agent
from kiro_crew import hooks, platform_compat
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.config.loader import data_home  # noqa: F401
from kiro_crew.config.paths import project_agents_dir  # noqa: F401
from kiro_crew.connections.mint import _MINT_AGENT_PREFIX  # noqa: F401
from kiro_crew.connections.mint import _MINT_NAME_RE  # noqa: F401
from kiro_crew.connections.mint import _mint_spec_body  # noqa: F401
from kiro_crew.connections.mint import _mint_watcher  # noqa: F401
from kiro_crew.connections.mint import _new_mint_token  # noqa: F401
from kiro_crew.connections.mint import (
    _MINT_GRANT_POLL_SECONDS,
    _MINT_TTL_SECONDS,
    MintState,
    _dispose_mint,
    _mints,
    _mints_lock,
)
from kiro_crew.connections.registry import is_preregistered  # noqa: F401
from kiro_crew.connections.registry import Provider, get_visible_providers
from kiro_crew.connections.tool_aliases import declared_tool_aliases  # noqa: F401
from kiro_crew.connections.tool_aliases import resolve_tool_aliases  # noqa: F401
from kiro_crew.connections.warm_runtime.generation_tree import _WARM_GENERATION_MARKER  # noqa: F401
from kiro_crew.connections.warm_runtime.generation_tree import _runtime_generation_dir  # noqa: F401
from kiro_crew.connections.warm_runtime.generation_tree import _spawn_left_no_process  # noqa: F401
from kiro_crew.connections.warm_runtime.generation_tree import _warm_generation_owner  # noqa: F401
from kiro_crew.connections.warm_runtime.generation_tree import _warm_work_dir  # noqa: F401
from kiro_crew.connections.warm_runtime.generation_tree import (  # noqa: F401
    _WARM_GENERATION_MARKER_MAX_BYTES,
    _WARM_GENERATION_MARKER_VERSION,
    _WARM_GENERATION_PREFIX,
    _WARM_GENERATION_SENTINEL,
    _WARM_RUNTIME_DIR_ATTR,
    _bind_warm_generation,
    _create_warm_generation_dir,
    _release_runtime_generation,
    _remove_warm_generation_dir,
    _warm_generation_agents_dir,
    _warm_generation_marker_path,
    _warm_generations_dir,
    _write_warm_generation_owner,
)
from kiro_crew.connections.warm_runtime.mint_process import (
    _acp_runtime_factory,
    _collect_oauth_requests,
    _destroy_session_quietly,
    _kill_quietly,
    _runtime_alive,
    _warm_session_mcp_servers,
    _WarmMintResult,
    _WarmSession,
)
from kiro_crew.connections.warm_runtime.rearms import _forget_invalidation_rearm  # noqa: F401
from kiro_crew.connections.warm_runtime.rearms import _invalidation_rearms  # noqa: F401
from kiro_crew.connections.warm_runtime.rearms import _rearm_invalidated_provider  # noqa: F401
from kiro_crew.connections.warm_runtime.rearms import rearm_invalidated_provider  # noqa: F401
from kiro_crew.connections.warm_runtime.rearms import (
    _rearm_dead_warm_mint,
    _settle_invalidation_rearms,
)
from kiro_crew.connections.warm_runtime.shared_rows import _claim_shared_mints  # noqa: F401
from kiro_crew.connections.warm_runtime.shared_rows import _dispose_displaced_rows  # noqa: F401
from kiro_crew.connections.warm_runtime.shared_rows import _mint_is_adopted  # noqa: F401
from kiro_crew.connections.warm_runtime.shared_rows import _mint_is_cold_held  # noqa: F401
from kiro_crew.connections.warm_runtime.shared_rows import _release_shared_claims  # noqa: F401
from kiro_crew.connections.warm_runtime.shared_rows import (
    _LIVE_STATES,
    _activations_in_use,
    _generation_holds_live_rows,
    _live_row_count,
    _shared_mints_pending,
    _warm_table_row,
)
from kiro_crew.connections.warm_runtime.spec_body import _WARM_NAME_RE  # noqa: F401
from kiro_crew.connections.warm_runtime.spec_body import _warm_spec_description  # noqa: F401
from kiro_crew.connections.warm_runtime.spec_body import connections_tool_aliases  # noqa: F401
from kiro_crew.connections.warm_runtime.spec_body import (
    _WARM_AGENT_PREFIX,
    _WARM_ALL_AGENT,
    _WARM_BASE_AGENT,
    _WARM_SPEC_SENTINEL,
    _is_stale_warm_spec,
    _warm_ownership_marks,
    _warm_spec_body,
)
from kiro_crew.connections.warm_runtime.spec_plan import _operator_oauth_client  # noqa: F401
from kiro_crew.connections.warm_runtime.spec_plan import (
    _auth_shape,
    _plan_is_servable,
    _registry_server_entry,
    _resident_roster_is_asked_for,
    _wanted_aliases,
    _warm_mintable_entry,
    _WarmSpecPlan,
)
from kiro_crew.connections.warm_runtime.start_identity import _CURRENT_START_ID_RE  # noqa: F401
from kiro_crew.connections.warm_runtime.start_identity import _marker_process_start_id  # noqa: F401
from kiro_crew.connections.warm_runtime.start_identity import _start_ids_comparable  # noqa: F401
from kiro_crew.connections.warm_runtime.start_identity import _process_identity_live
from kiro_crew.connections.warm_runtime.table_flow import _absorb_warm_requests  # noqa: F401
from kiro_crew.connections.warm_runtime.table_flow import _adopt_shared_row  # noqa: F401
from kiro_crew.connections.warm_runtime.table_flow import _providers_with_requests  # noqa: F401
from kiro_crew.connections.warm_runtime.table_flow import _recover_redrained_requests  # noqa: F401
from kiro_crew.connections.warm_runtime.table_flow import adopt_shared_mint  # noqa: F401
from kiro_crew.connections.warm_runtime.table_flow import warm_mint_all  # noqa: F401
from kiro_crew.mcp_discovery import list_servers
from kiro_crew.mcp_grant import grant_presence as grant_present
from kiro_crew.mcp_utils import kiro_entry_client_id  # noqa: F401
from kiro_crew.mcp_utils import kiro_entry_scopes  # noqa: F401
from kiro_crew.mcp_utils import kiro_oauth_wire_entry  # noqa: F401
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.security import oauth_url_contains_credential
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_WARM_SPAWN_TIMEOUT_SECONDS = 90.0
_WARM_SESSION_TIMEOUT_SECONDS = 90.0
_WARM_SESSION_DESTROY_TIMEOUT_SECONDS = 10.0
#: How long to keep waiting for a handle whose create we already gave up on. Short, because
#: this only buys back a session already on its way; see ``_abandon_session_creation``.
_WARM_SESSION_REAP_TIMEOUT_SECONDS = 10.0
_WARM_KILL_TIMEOUT_SECONDS = 20.0
#: One bounded drain window. A run stops after this many consecutive quiet windows,
#: while each newly observed provider renews that quiet budget. The absolute cap scales by
#: the expected roster, so unique progress cannot extend collection without bound.
_WARM_OAUTH_SETTLE_SECONDS = 0.5
_WARM_OAUTH_SETTLE_ROUNDS = 6
#: A tenth of the mint TTL: long enough that reopening the gallery reuses the
#: process, short enough that an abandoned visit leaves no kiro-cli resident.
_WARM_IDLE_GRACE_SECONDS = _MINT_TTL_SECONDS / 10
#: One respawn, then the cold path -- a second death means the process cannot stay
#: up, and a Connect is better served by its own dedicated spawn.
_WARM_ACTIVATION_ATTEMPTS = 2
#: One automatic replacement per explicit page warm. The replacement does not re-arm this
#: budget, so a repeatedly dying process cannot turn the reaper into a restart loop.
_WARM_REARM_ATTEMPTS = 1
#: Generation key for a process that never became ``self._runtime`` -- an abandoned spawn,
#: which owns no rows. NEGATIVE because generations only ever increment from zero, so no row
#: can carry it: ``_generation_holds_live_rows`` reads it as needed by nobody, and the sweep
#: retries its kill instead of parking it forever behind a row that will never appear.
_WARM_UNKEYED_GENERATION = -1


class _WarmMintUnsafe(RuntimeError):
    """A warm mint was about to be issued in a way that kills the shared process."""


class _WarmMintDied(RuntimeError):
    """The shared process was gone by the end of an activation."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _log_warm_event(operation: str, resources: str, outcome: str = "ok") -> None:
    """Record a warm-table event. Never carries a URL or an exception message."""
    sel().log_api_access(
        caller="dashboard",
        operation=operation,
        outcome=outcome,
        source="dashboard",
        resources=resources,
    )


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
    """The subset of ``universe`` an activation should actually ask a URL for.

    A DEFINITIVE absence -- ``is False`` -- never a falsy answer. ``grant_present`` is
    tri-state and ``None`` means the grant cache could not be read at all, which a
    truthiness test reads as "no grant": consent is then initiated on an absence nobody
    confirmed, and the card is flipped to waiting behind an approval URL for a provider
    that may already be connected. L1 collapses the same third answer deliberately because
    it never initiates consent; this path does, so it must not.

    Each provider is stat-ed exactly ONCE here. Re-reading presence to classify the skip is
    the two-pass race :func:`~kiro_crew.mcp_grant.grant_presence` refuses -- a failure that
    cleared between the passes would read as a definitive absence and warm anyway.

    An indeterminate provider is skipped for this activation, not dropped forever: the cold
    path still serves its Connect, and the next scan re-reads the cache.
    """
    presence = [(provider, grant_present(provider["mcp_url"])) for provider in universe]
    unknown = sorted(provider["slug"] for provider, verdict in presence if verdict is None)
    if unknown:
        logger.warning(
            "Not warming %d provider(s) whose grant cache could not be read, so absence is "
            "unconfirmed: %s. The cold path still serves their Connect.",
            len(unknown),
            ", ".join(unknown),
        )
    return [provider for provider, verdict in presence if verdict is False]


def _warm_candidate_scan() -> tuple[list[Provider], list[Provider]]:
    """``(spec universe, activation candidates)`` from one pass over the registry."""
    try:
        universe = warm_spec_providers()
    except Exception:  # noqa: BLE001 — reads user config; degrade to warming nothing
        logger.debug("warm mint inventory read failed", exc_info=True)
        return [], []
    return universe, _warm_activation_candidates(universe)


def _current_warm_redrain_entry(slug: str) -> dict[str, Any] | None:
    """The current eligible authorization entry for a late-session re-drain.

    Rebuild the same candidate plan activation uses rather than trusting the provider
    snapshot retained by an older session. This rechecks visibility, enabled state, the
    tri-state grant verdict, configured-entry compatibility, and the registry auth shape.
    The caller runs this off the event loop because those checks read user-owned files.
    """
    _universe, candidates = _warm_candidate_scan()
    provider = next((item for item in candidates if item["slug"] == slug), None)
    if provider is None:
        return None
    return _warm_spec_plan([provider]).entries.get(mcp_server_alias(slug))


_GRANT_PRESENCE_READ_ID = "connections_premint.oauth_grant_presence"


def mintable_providers() -> list[Provider]:
    """Providers an activation should warm right now, registry order."""
    return _warm_candidate_scan()[1]


def _audited_mintable_providers() -> tuple[list[Provider], bool]:
    """Return candidates and whether their acted-on grant scan was SEL-audited.

    The explicit page warm and its one automatic replacement are one page-owned
    lifecycle, so both use the premint read id. An empty scan drives no action and
    therefore owes no event.
    """
    candidates = mintable_providers()
    if not candidates:
        return candidates, True
    recorded = hooks.emit_internal_read_audit(_GRANT_PRESENCE_READ_ID, "success")
    return candidates, recorded


def _warm_spec_plan(providers: list[Provider]) -> _WarmSpecPlan:
    """Build (but do not write) the warm process's spec set."""
    agents_dir = _agent.kiro_agents_dir_path()
    # Through the hardened reader: the agents dir is user-writable and
    # shared with other tools, so a symlink planted at this path pointed a raw ``_load_json``
    # at a file outside it -- followed, parsed, uncapped and unaudited -- and the contents
    # then DECIDED the plan, because a configured entry vetoes its provider below. A refusal
    # reads as an absent file: planning proceeds with nothing configured, which vetoes
    # nothing, rather than failing the warm path.
    spec = _read_agent_spec(
        agents_dir / AGENT_FILENAME,
        operation="connections_warm_mint",
        source="dashboard",
    )
    configured = (spec or {}).get("mcpServers") or {}
    entries: dict[str, dict[str, Any]] = {}
    for provider in providers:
        alias = mcp_server_alias(provider["slug"])
        entry = _warm_mintable_entry(provider, configured.get(alias))
        if entry is None:
            continue
        entries[alias] = entry

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
    digest = hashlib.sha256(
        json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _WarmSpecPlan(
        all_agent=_WARM_ALL_AGENT if entries else "",
        specs=specs,
        entries=entries,
        digest=digest,
    )


def _private_warm_spec_work_dir(path: Path) -> Path | None:
    """Return the owned generation root for a project-local spec, or ``None``.

    The ordinary agent reader rejects ``data_home/run`` by design. These files live there
    deliberately, so a separate reader is allowed only after the path is proved to be a
    direct child of a sentinel-owned generation scope.
    """
    agents_dir = path.parent
    if agents_dir.name != "agents" or agents_dir.parent.name != ".kiro":
        return None
    work_dir = agents_dir.parent.parent
    if not _is_plain_warm_generation_dir(work_dir):
        return None
    return work_dir if _read_warm_generation_owner(work_dir) is not None else None


def _read_warm_spec_body(path: Path) -> dict[str, Any] | None:
    """Read one spec through the correct trust boundary for its scope."""
    if _private_warm_spec_work_dir(path) is None:
        return _read_agent_spec(
            path,
            operation="connections_warm_mint",
            source="dashboard",
        )
    try:
        if platform_compat.is_link_or_junction(path):
            return None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > hooks.MAX_FILE_BYTES:
            return None
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _warm_spec_is_foreign(path: Path) -> bool:
    """True when ``path`` exists but no warm plan wrote it, so this module must not touch it.

    Warm mode names are FIXED and predictable, so the name shape
    :func:`_is_stale_warm_spec` checks says where a spec of ours would GO -- never that the
    file already there is one. Production writes them inside a private generation, while
    startup also applies this predicate to old global paths. Ownership is proved from the
    contents in both scopes: the declared ``name`` matches the file with every field the
    writer fixes still holding, AND the description carries the sentinel. The marks alone
    are generic defaults, so the sentinel is the half that discriminates.

    Fails closed, because the two mistakes are not symmetric. Reading a file of ours as
    foreign costs one stale spec left as clutter; reading a user's hand-written agent as
    ours deletes it, or overwrites it with a spec they never asked for. So a file that is
    unreadable, not a JSON object, or shaped like anything but our own is foreign.
    """
    # Global/user files go through the hardened discovery reader. Private generation files
    # use the equally bounded non-link reader above only after their sentinel-owned run root
    # is proved; the general reader correctly rejects that sensitive path.
    body = _read_warm_spec_body(path)
    if not body:
        # A refusal reads as an empty body: absent, unreadable, non-object, oversized and a
        # sensitive-target symlink all land here, and only the ABSENT one is a path we may
        # write -- which is what the presence check separates. A dangling symlink resolves
        # to nothing, so ``exists()`` alone would report the path free and the writer would
        # replace the link (and the sweep unlink it) -- destroying a path occupant this
        # module does not own; the link itself occupying the path is what counts. Every
        # refusal therefore reads as foreign, which is refused and left in place: the safe
        # direction.
        #
        # ``is_link_or_junction``, not ``Path.is_symlink()``: a Windows directory JUNCTION
        # is a reparse point ``is_symlink()`` answers False for (and answers False to
        # ``exists()`` when dangling), and it is the only directory link an unprivileged
        # Windows writer can plant here. An ``is_symlink()`` guard read a junction at this
        # name as a free path, so the writer replaced it and the sweep unlinked it --
        # destroying an occupant this module does not own.
        return platform_compat.is_link_or_junction(path) or path.exists()
    marks = _warm_ownership_marks(path.stem)
    if body.get("name") != path.stem or any(body.get(key) != value for key, value in marks.items()):
        return True
    # An unsentinelled global file remains foreign even if an early/dev build produced it.
    # Startup logs and retains that path because its name cannot prove ownership; the design
    # note gives the operator the manual inspection/removal rule. Private generation specs
    # always carry the sentinel before their process starts.
    return not str(body.get("description") or "").startswith(_WARM_SPEC_SENTINEL)


def _read_warm_generation_owner(work_dir: Path) -> dict[str, Any] | None:
    """Read a small plain ownership marker; malformed or linked input is unowned."""
    marker = _warm_generation_marker_path(work_dir)
    try:
        if platform_compat.is_link_or_junction(marker):
            return None
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _WARM_GENERATION_MARKER_MAX_BYTES:
            return None
        body = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("sentinel") != _WARM_GENERATION_SENTINEL:
        return None
    if body.get("version") != _WARM_GENERATION_MARKER_VERSION:
        return None
    return body


def _is_plain_warm_generation_dir(work_dir: Path) -> bool:
    """Whether *work_dir* is one direct, non-link child of the managed root."""
    if work_dir.parent != _warm_generations_dir() or not work_dir.name.startswith(
        _WARM_GENERATION_PREFIX
    ):
        return False
    try:
        if platform_compat.is_link_or_junction(work_dir):
            return False
        return stat.S_ISDIR(work_dir.lstat().st_mode)
    except OSError:
        return False


def _recorded_runtime_is_dead(work_dir: Path) -> bool:
    """Whether the marker positively proves its runtime is gone.

    Mirrors the scavenger's ``is not False`` test, so only proof of death releases a tree and
    both "still running" and "cannot tell" keep it. An absent or unreadable marker answers
    ``True`` here only because :func:`_remove_warm_generation_dir` independently refuses an
    unowned directory, which keeps the ownership refusal in one place. A marker still
    carrying ``runtime_pid: 0`` is the PROVISIONAL write from
    :func:`_create_warm_generation_dir` whose post-spawn rewrite in
    :func:`_bind_warm_generation` never landed -- the spawned process may be alive with no
    recorded identity to check, so pid 0 answers ``False`` (unproved keeps the tree), exactly
    as :func:`_process_identity_live` answers ``None`` for the same marker on the scavenge
    path.
    """
    owner = _read_warm_generation_owner(work_dir)
    if owner is None:
        return True
    try:
        pid = int(owner.get("runtime_pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    return _process_identity_live(pid, str(owner.get("runtime_started") or "")) is False


def _scavenge_warm_generation_dirs() -> int:
    """Remove crash leftovers only when both recorded process identities are dead."""
    root = _warm_generations_dir()
    try:
        candidates = list(root.iterdir())
    except FileNotFoundError:
        return 0
    except OSError:
        logger.debug("warm generation startup scan failed", exc_info=True)
        return 0
    removed = 0
    for work_dir in candidates:
        if not _is_plain_warm_generation_dir(work_dir):
            continue
        owner = _read_warm_generation_owner(work_dir)
        if owner is None:
            logger.warning(
                "Keeping unproved warm generation directory %s; inspect it manually",
                work_dir.name,
            )
            continue
        try:
            gateway_live = _process_identity_live(
                int(owner.get("gateway_pid") or 0), str(owner.get("gateway_started") or "")
            )
            runtime_live = _process_identity_live(
                int(owner.get("runtime_pid") or 0), str(owner.get("runtime_started") or "")
            )
        except (TypeError, ValueError):
            gateway_live = runtime_live = None
        if gateway_live is not False or runtime_live is not False:
            continue
        if _remove_warm_generation_dir(work_dir):
            removed += 1
    if removed:
        logger.info("Removed %d stale private warm generation directorie(s)", removed)
    return removed


def _write_warm_mint_specs(plan: _WarmSpecPlan, agents_dir: Path) -> None:
    """Write the complete plan into one private project-local agent scope.

    Every unlink and write is still ownership-gated. The directory is generation-private,
    but preserving the content proof closes same-user races and keeps the legacy cleanup
    predicate identical to the writer predicate.
    """
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


def _unowned_plan_specs(plan: _WarmSpecPlan, agents_dir: Path) -> list[str]:
    """Planned names missing from, or foreign to, this generation's private scope."""
    unowned: list[str] = []
    for name in plan.specs:
        path = agents_dir / f"{name}.json"
        if not path.is_file() or _warm_spec_is_foreign(path):
            unowned.append(name)
    return unowned


def _remove_warm_mint_specs(agents_dir: Path) -> int:
    """Unlink every sentinel-owned warm spec from one explicit agent scope."""
    removed = 0
    try:
        for path in agents_dir.glob(f"{_WARM_AGENT_PREFIX}*.json"):
            if not _is_stale_warm_spec(path.stem, frozenset()):
                continue
            if _warm_spec_is_foreign(path):
                _log_warm_event("warm_mint_spec_removal", path.name, outcome="refused")
                continue
            path.unlink(missing_ok=True)
            removed += 1
    except Exception:  # noqa: BLE001 — startup cleanup is best-effort and fail-closed
        logger.debug("warm mint spec removal failed", exc_info=True)
    return removed


def scavenge_warm_mint_artifacts() -> int:
    """Boot cleanup for private crash leftovers and sentinel-owned global legacy specs.

    An unsentinelled global file is deliberately retained: its name does not prove Kiro Crew
    owns it. The design note documents the manual inspection/removal path for that legacy
    residue.
    """
    removed = _scavenge_warm_generation_dirs()
    removed += _remove_warm_mint_specs(_agent.kiro_agents_dir_path())
    return removed


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
        #: Armed only by an explicit page warm. A replacement inherits the lifecycle but not
        #: another attempt, which bounds unattended recovery to one process per page request.
        self._supervision_armed = False
        self._rearms_remaining = 0
        #: Strong reference and single-flight identity for a replacement activation. The
        #: reaper can cancel itself while the child replaces its dead generation, so the task
        #: cannot be owned only by the awaiting reaper.
        self._rearm_task: asyncio.Task[list[str]] | None = None

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

    def arm_supervision(self) -> None:
        """Allow one automatic replacement for the current explicit page warm."""
        self._supervision_armed = True
        self._rearms_remaining = _WARM_REARM_ATTEMPTS

    def start_rearm(self, generation: int) -> asyncio.Task[list[str]] | None:
        """Return the one replacement task for ``generation``, or decline recovery.

        The state transition is synchronous, so concurrent observers on the event loop either
        receive the same task or see the budget already consumed. A generation mismatch means
        another path already replaced the dead process; a held runtime lock means an explicit
        warm is already deciding the replacement and must not be followed by a second session.
        """
        task = self._rearm_task
        if task is not None and not task.done():
            return task
        if task is not None:
            self._rearm_task = None
        if (
            not self._supervision_armed
            or self._rearms_remaining <= 0
            or generation != self._generation
            or self.is_alive()
            or self._lock.locked()
        ):
            return None
        self._rearms_remaining -= 1
        task = asyncio.get_running_loop().create_task(_rearm_dead_warm_mint(generation))
        self._rearm_task = task
        task.add_done_callback(self._finish_rearm)
        return task

    def _finish_rearm(self, task: asyncio.Task[list[str]]) -> None:
        """Release the single-flight slot and surface an otherwise-unobserved failure."""
        if self._rearm_task is task:
            self._rearm_task = None
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.debug(
                "warm mint automatic re-arm failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    def _disarm_supervision(self) -> asyncio.Task[list[str]] | None:
        """Prevent resurrection and detach the task shutdown must settle."""
        self._supervision_armed = False
        self._rearms_remaining = 0
        task, self._rearm_task = self._rearm_task, None
        return task

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
                destroyed = await _destroy_session_quietly(record.handle)
            finally:
                # Forgotten only once the destroy actually TOOK. The record is the only
                # reference left to the handle, so losing it while the session is still
                # listening leaves it -- and the loopback servers it owns -- with nothing
                # that could ever retry; the record stays settled and expired, so the next
                # reaper sweep is that retry. Held across the await deliberately: a reader
                # without the lock then over-reports the session as live for a moment, which
                # keeps a row waiting a beat longer rather than withdrawing a URL, the safe
                # direction.
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

    async def mint_for(self) -> _WarmMintResult | None:
        """Ensure a live process, activate it, return its challenges."""
        async with self._lock:
            await self._sweep_retiring_locked()
            universe, candidates = await asyncio.to_thread(_warm_candidate_scan)
            # The UNIVERSE decides what the process must have enumerated; the CANDIDATES
            # decide what this activation asks for. Apart, a grant moves the second only.
            plan = await self._ensure_locked(universe)
            if plan is None:
                return None
            agent = plan.all_agent
            if not agent:
                return None
            # Keyed on the plan's own roster, which is what the activated mode will mount:
            # a candidate the plan vetoed must not be asked for even though the scan found
            # it. Alias, not slug, because the roster and the activation both work in
            # aliases (``_wanted_aliases`` below).
            wanted = [
                provider
                for provider in candidates
                if mcp_server_alias(provider["slug"]) in plan.entries
            ]
            if not wanted:
                return None
            generation = self._generation
            try:
                activation, requests = await self._activate_locked(
                    agent, _wanted_aliases(wanted), tuple(wanted)
                )
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

    async def _ensure_locked(self, providers: list[Provider]) -> _WarmSpecPlan | None:
        """Guarantee a process whose specs can serve ``providers``, and return THAT plan.

        The returned plan -- never ``self._plan`` -- is what an activation must be driven
        from. The two differ on the reuse path: ``self._plan`` is what the running process
        ENUMERATED, which a shrink leaves a SUPERSET of what this scan wants, so it still
        names providers the scan excluded. ``_warm_mintable_entry`` excludes a provider
        whose CONFIGURED entry diverges from the registry precisely because only the cold
        path can honour it without handing back a grant the user did not ask for -- and an
        activation driven from the resident plan asked for that provider's URL anyway, with
        the registry's scopes and client id, bypassing the exclusion entirely.

        So the two reads are split by job: servability decides whether to RESPAWN, this plan
        decides what to ASK FOR. ``None`` when nothing can be warmed.

        Reuse takes BOTH tests. Servability says the resident process can answer for every
        provider this scan wants; :func:`_resident_roster_is_asked_for` says it will not also
        mount one the scan excluded. Nothing narrows a mode's mounted set after the spawn that
        enumerated it, so this decision is the only place the exclusion can be enforced.
        """
        try:
            plan = await asyncio.to_thread(_warm_spec_plan, providers)
        except Exception:  # noqa: BLE001 — reads user-editable JSON; never fail a mint
            logger.debug("warm mint spec plan failed", exc_info=True)
            return None
        if not plan.all_agent:
            return None

        if self.is_alive() and self._digest == plan.digest:
            # Digest equality is spec-set equality, so every mode's roster is asked for.
            return plan
        resident = self._plan
        if (
            self.is_alive()
            and resident is not None
            and _plan_is_servable(resident, plan)
            and _resident_roster_is_asked_for(resident, plan)
        ):
            logger.info(
                "Shared mint generation %d already serves the new candidate set; "
                "re-activating instead of respawning",
                self._generation,
            )
            return plan
        if self._runtime is not None:
            logger.info(
                "Starting a new shared mint generation (%s)",
                "specs are incompatible" if self.is_alive() else "process is gone",
            )
        await self._park_or_kill_locked()

        runtime: Any = None
        work_dir: Path | None = None
        handed_over = False
        try:
            work_dir = await asyncio.to_thread(_create_warm_generation_dir)
            agents_dir = _warm_generation_agents_dir(work_dir)
            await asyncio.to_thread(_write_warm_mint_specs, plan, agents_dir)
            # The write REFUSES a path it does not own rather than clobbering it, and the
            # spawn below activates by NAME -- so without this the refusal would hand
            # kiro-cli a stranger's spec to execute. Aborting is safe and honest: the cold
            # path still serves every Connect, it just spawns per provider.
            unowned = await asyncio.to_thread(_unowned_plan_specs, plan, agents_dir)
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
                return None
            runtime = _acp_runtime_factory()(
                work_dir=work_dir,
                agent=_WARM_BASE_AGENT,
                sandbox_mode="auto",
            )
            # Attach before spawn so a cancelled/failed spawn still carries the only path its
            # cleanup may remove. The marker remains provisional until spawn returns a PID.
            setattr(runtime, _WARM_RUNTIME_DIR_ATTR, str(work_dir))
            await asyncio.wait_for(runtime.spawn(), timeout=_WARM_SPAWN_TIMEOUT_SECONDS)
            await asyncio.to_thread(_bind_warm_generation, runtime, work_dir)
            # No await between the ownership marker landing and the handover: the runtime,
            # plan and reaper become visible as one event-loop-atomic state transition.
            self._runtime, self._plan, self._digest = runtime, plan, plan.digest
            self._generation += 1
            self._reaper = asyncio.get_running_loop().create_task(
                _warm_mint_reaper(self._generation)
            )
            handed_over = True
        except Exception as exc:  # noqa: BLE001 — degrade to the cold path
            # Carry the exception's OWN message, not only its class: AcpRuntimeError,
            # SandboxUnavailableError and the spawn gates each author a sentence that
            # names what failed and the remedy, and the cold path that follows hides the
            # same failure per provider, so the class name alone leaves an operator no
            # way to tell a missing kiro-cli from a sandbox a host cannot provide. The
            # message text these classes build is operator-facing and carries no
            # credential; the full traceback goes to debug for a developer.
            logger.warning("Shared mint process spawn failed: %s: %s", type(exc).__name__, exc)
            logger.debug("Shared mint process spawn failed", exc_info=True)
            return None
        finally:
            # A ``finally``, not an ``except Exception``: the stand-down above has already
            # parked the old generation AND cancelled its reaper, so a CancelledError in the
            # spec write or the spawn would otherwise leave that process with nothing that
            # will ever sweep it -- the parked-generation leak by a third route -- plus a
            # forked child and a private spec tree nobody owns.
            if not handed_over:
                await self._abandon_spawn_locked(runtime, work_dir)
        await asyncio.to_thread(
            _log_warm_event,
            "connections_warm_mint_spawn",
            f"providers:{len(plan.entries)} generation:{self._generation}",
        )
        return plan

    async def _abandon_spawn_locked(
        self,
        runtime: Any,
        work_dir: Path | None = None,
    ) -> None:
        """Undo a spawn attempt that never became this object's process.

        Reached from a ``finally``, so it covers cancellation as well as failure. A process
        that may still live keeps its directory attached while the parked-generation drain
        retries the kill; a directory created before any runtime existed can be released
        immediately.
        """
        if self._retiring:
            # Armed BEFORE any await, because ``create_task`` is synchronous: the parked
            # generation then has its sweeper even if the teardown below is interrupted.
            # Stored as the reaper so a later spawn's own reaper replaces it.
            self._reaper = asyncio.get_running_loop().create_task(_drain_parked_generations())
        if runtime is None:
            if work_dir is not None:
                await asyncio.to_thread(_remove_warm_generation_dir, work_dir)
            return
        if not await _kill_quietly(runtime):
            # This attempt never became ``self._runtime``, so it owns no generation and no
            # rows. Tracked under a key no row can carry, which makes every sweep read it as
            # needed by nobody and retry the kill until it takes -- rather than dropping the
            # last reference to a child that outlived the spawn we gave up on.
            self._retain_unkilled_locked(_WARM_UNKEYED_GENERATION, runtime)
            return
        await asyncio.to_thread(_release_runtime_generation, runtime)

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
            destroyed = await _destroy_session_quietly(handle)
        finally:
            # Registered settled and already expired above, so a record kept here because
            # the destroy did not take is picked up by the next sweep rather than lost.
            if destroyed:
                self._sessions.pop(activation, None)

    async def _activate_locked(
        self,
        agent: str,
        wanted: frozenset[str],
        providers: tuple[Provider, ...] = (),
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
            providers=providers,
        )
        try:
            requests = await _collect_oauth_requests(handle, wanted)
        except BaseException:
            # Nothing will ever be stamped with this activation, so the session it
            # registered would leak past the sweep's settled-only rule. Marked settled and
            # already expired FIRST, so that if the destroy below is interrupted -- or does
            # not take -- the sweep is a real retry; and popped only once the destroy
            # actually completed, because the record is the only reference left to the
            # handle.
            record = self._sessions.get(activation)
            if record is not None:
                record.settled = True
                record.expires_at = 0.0
            if await _destroy_session_quietly(handle):
                self._sessions.pop(activation, None)
            raise
        return activation, requests

    async def redrain_for(self, slug: str) -> _WarmMintResult | None:
        """Drain a current-compatible live session for ``slug`` without spawning another."""
        current_entry = await asyncio.to_thread(_current_warm_redrain_entry, slug)
        if current_entry is None:
            return None
        async with self._lock:
            for activation, record in reversed(list(self._sessions.items())):
                if not self.generation_is_live(record.generation):
                    continue
                activated_provider = next(
                    (provider for provider in record.providers if provider["slug"] == slug),
                    None,
                )
                if activated_provider is None:
                    continue
                # Off the loop: for a pre-registered provider this reads
                # config.json and decrypts the vault (`_operator_oauth_client`),
                # the same file work the current-entry read above offloads.
                activated_entry = await asyncio.to_thread(
                    _registry_server_entry, activated_provider
                )
                if activated_entry is None or _auth_shape(activated_entry) != _auth_shape(
                    current_entry
                ):
                    return None
                requests = await _collect_oauth_requests(
                    record.handle, frozenset({mcp_server_alias(slug)})
                )
                if not requests:
                    return None
                return _WarmMintResult(
                    generation=record.generation,
                    activation=activation,
                    providers=[activated_provider],
                    requests=requests,
                )
        return None

    async def shutdown(self) -> None:
        """Disarm recovery, settle its task, then retire every owned process."""
        rearm = self._disarm_supervision()
        try:
            if rearm is not None and rearm is not asyncio.current_task() and not rearm.done():
                rearm.cancel()
                # ``return_exceptions`` consumes the CHILD cancellation. Cancellation of this
                # shutdown still raises from gather and reaches the hard teardown in finally.
                await asyncio.gather(rearm, return_exceptions=True)
        finally:
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
                if not await self._kill_generation(generation, runtime):
                    # The child may still be running, so this generation is NOT retired:
                    # left in ``drop`` for the ``finally`` to put back and a later sweep to
                    # retry. Stopping here rather than continuing keeps the list's order.
                    break
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
            if not await self._kill_generation(generation, runtime):
                # The kill did not take. Same reasoning as the handler below, reached for a
                # plain failure (a timeout) rather than a cancellation.
                self._retain_unkilled_locked(generation, runtime)
        except BaseException:
            # ``_runtime`` and the reaper were cleared synchronously above, so this list is
            # now the ONLY reference to a process the kill did not finish with. Park it and
            # arm the drain rather than dropping it: an untracked child never gets retired.
            self._retain_unkilled_locked(generation, runtime)
            raise

    async def _kill_generation(self, generation: int, runtime: Any) -> bool:
        """Kill one process, expire its links, then release its private spec tree.

        ``False`` when the kill did not take, and the caller must keep the pair tracked. The
        rows are expired either way -- this generation is being retired, so its URLs must
        stop being served -- while the generation directory stays attached until a confirmed
        kill. A parked or unkillable process therefore never loses the modes it still owns.
        """
        killed = await _kill_quietly(runtime)
        self._drop_generation_sessions(generation)
        await _expire_shared_mints("mint_process_gone", generation=generation)
        if killed:
            await asyncio.to_thread(_release_runtime_generation, runtime)
        return killed

    def _retain_unkilled_locked(self, generation: int, runtime: Any) -> None:
        """Keep a process whose kill did not take, and make sure something retries it.

        Every caller clears ``_runtime`` and cancels the reaper synchronously BEFORE the
        kill, so this list is then the only reference to a child that may still be alive.
        Arming the drain is what performs the retry: it re-sweeps while anything is parked.
        """
        self._retiring.append((generation, runtime))
        if self._reaper is None:
            self._reaper = asyncio.get_running_loop().create_task(_drain_parked_generations())

    async def _retire_locked(self) -> None:
        """Hard teardown: every generation, parked ones included."""
        reaper, runtime, generation = self._reaper, self._runtime, self._generation
        # Parked generations first, then the current one -- a parked process is the one a
        # card may still be mid-consent on, so it is retired before the live one.
        pending = list(self._retiring)
        if runtime is not None:
            pending.append((generation, runtime))
        self._retiring = []
        self._reaper = self._runtime = None
        self._plan, self._digest = None, ""
        self._sessions.clear()
        if reaper is not None and reaper is not asyncio.current_task():
            reaper.cancel()
        try:
            while pending:
                doomed_generation, doomed_runtime = pending[0]
                killed = await _kill_quietly(doomed_runtime)
                await _expire_shared_mints("mint_process_gone", generation=doomed_generation)
                if not killed:
                    # A hard teardown that could not kill a child must not report it retired:
                    # left in ``pending`` for the ``finally`` below to re-track, with its
                    # private agent scope still attached.
                    break
                await asyncio.to_thread(_release_runtime_generation, doomed_runtime)
                # Popped only once this generation and its private scope are fully retired.
                pending.pop(0)
        finally:
            if pending:
                # Same rule as everywhere else in this class: a process whose teardown did
                # not complete stays tracked, with something scheduled to retire it. The
                # lists above were emptied synchronously, so this is the only reference left.
                self._retiring = pending
                self._reaper = asyncio.get_running_loop().create_task(_drain_parked_generations())


_warm_mint = _WarmMintRuntime()


async def shutdown_warm_mint() -> None:
    """Gateway cleanup hook: settle every scheduled re-arm, then retire every generation."""
    try:
        await _settle_invalidation_rearms()
    finally:
        await _warm_mint.shutdown()


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


async def _expire_shared_mints(reason: str, *, generation: int | None = None) -> list[str]:
    """Flip live shared mints stale. Called when a process is gone.

    ``generation`` is the only narrowing there is, and every caller passes it: the rows a
    dead process cannot redeem are exactly the ones it minted. Narrowing by the
    CALLER's own row tokens instead reads as "spare my retry" but means "expire every
    other generation", which withdraws a parked generation's redeemable URL.
    Withdrawal follows the verifier, so it follows the generation.
    """
    flipped: list[str] = []
    async with _mints_lock:
        for slug, entry in _mints.items():
            if not _warm_table_row(entry) or entry.get("state") not in _LIVE_STATES:
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
    """Withdraw every warm-held row whose holding process is gone. THE chokepoint.

    Adopted rows included, and that is not incidental: the cold engine's
    ``_mint_holder_alive`` deliberately ABSTAINS on a row carrying a ``generation``
    (it owns no ``client`` to read), so this is the only reader that can withdraw one.
    """
    doomed: list[str] = []
    async with _mints_lock:
        for slug, entry in _mints.items():
            if not _warm_table_row(entry) or entry.get("state") != "waiting":
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


async def _warm_activate() -> _WarmMintResult | None:
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
            return await _warm_mint.mint_for()
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
                rearm = _warm_mint.start_rearm(generation)
                if rearm is not None:
                    # Shielded because replacing the dead generation cancels THIS reaper.
                    # The runtime owns the strong reference, and the replacement must finish
                    # even after the obsolete observer exits.
                    try:
                        await asyncio.shield(rearm)
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001 -- the cold path remains the fallback
                        logger.debug("warm mint automatic re-arm failed", exc_info=True)
                    if _warm_mint.is_alive():
                        return
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


def _credential_bearing_slugs(urls: dict[str, str]) -> set[str]:
    """The slugs in ``{slug: url}`` whose approval URL carries a credential.

    The gate is :func:`~kiro_crew.security.oauth_url_contains_credential` -- the same
    predicate the cold mint and the chat consent banner apply -- and it is synchronous
    because it can consult the operator's on-disk OAuth-endpoint extension, an unbounded
    stat on a network mount. Never returns or logs the value it judged, and it does not
    name the endpoint either: a refused slug's claim is RELEASED (see
    ``_absorb_warm_requests``), the card then asks for a cold mint, and that mint hits the
    same URL and names the endpoint through ``mint.py``'s ``mint_url_rejected`` card view.
    The warm surface stays slug-only by design -- a screened premint is not a terminal
    failure, and the logger is not a channel for URL-derived text.
    """
    return {slug for slug, url in urls.items() if url and oauth_url_contains_credential(url)}

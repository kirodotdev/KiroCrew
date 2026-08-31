"""Session registry, allocation, and claim coordination.

``SessionAllocationService`` owns the live-session registry and every lock or
lease needed to allocate from it.  Warm-pool inventory, compaction, teardown,
and cleanup policy remain separate owner-facade responsibilities.  This module
never imports :mod:`kiro_crew.session` at runtime; patchable compatibility seams
are supplied through :class:`AllocationDeps`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from kiro_crew.config.paths import CWD_CLEARED, resolved_cwd
from kiro_crew.member_memory_auth import private_memory_store_for_session
from kiro_crew.metrics.sessions import (
    END_REASON_EVICTED,
    discard_session_start,
    record_session_ended,
    record_session_started,
)

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider
else:
    # ProviderFactory below subscripts LLMProvider at module scope, so a name must
    # exist at runtime; a real import would cross the agent-SDK boundary gate.
    LLMProvider = Any


ProviderFactory = Callable[..., LLMProvider]


class SessionClosingError(RuntimeError):
    """A turn was requested after manager shutdown began."""


class SessionBusyError(RuntimeError):
    """A caller requested an immediate turn claim while the session was held."""


class SpeculativeResumeRefused(RuntimeError):
    """A speculative allocation may not consume an unrequested native resume."""


@dataclass(frozen=True, slots=True)
class AllocationConstants:
    """Behavioral constants supplied by the facade's patchable namespace."""

    max_concurrent_cold_starts: int
    won_race_max_retries: int
    circuit_breaker_threshold: int
    agent_model_cache_ttl: Callable[[], float]
    background_key: str
    heartbeat_key: str
    background_agent: str
    subagent_prefix: str
    stateless_prefixes: tuple[str, ...]
    provider_label_default: str
    provider_label_claude: str


@dataclass(frozen=True, slots=True)
class AllocationDeps:
    """Injected leaf dependencies and dynamic compatibility seams.

    Functions whose source names are monkeypatched in existing tests should be
    passed as forwarding lambdas.  The service deliberately holds no copy of
    ``SessionMap``: the owner's live instance remains persistence authority.
    """

    logger: logging.Logger
    constants: AllocationConstants
    canonical_key: Callable[[str], str]
    legacy_key: Callable[[str], str | None]
    provider_has_active_turn: Callable[[LLMProvider], bool]
    provider_effectively_alive: Callable[[LLMProvider], bool]
    is_acp_provider: Callable[[LLMProvider], bool]
    is_claude_provider: Callable[[LLMProvider], bool]
    is_claude_backend: Callable[[LLMProvider], bool]
    provider_label: Callable[[LLMProvider], str]
    detect_provider_switch: Callable[[Any, str, str], bool]
    session_factory: Callable[..., Any]
    first_turn_nothing_armed: object
    first_turn_fresh: object
    first_turn_resumed: object
    runtime_types: Callable[[], tuple[Callable[..., Any], type[BaseException]]]
    session_provider_type: Callable[[], Callable[[Any, Any], LLMProvider]]
    unlink_session_queue: Callable[[Any], None]
    unlink_queued_temp_paths: Callable[[dict[str, Any]], None]
    session_model: Callable[[Any, str | None], str | None]
    load_config: Callable[[], Any]
    resolve_crew_identity: Callable[[Any, str | None, str | None], str]
    load_watchdog_settings: Callable[[str], object]
    advertised_model_ids: Callable[[Any], list[str]]
    model_is_unusable: Callable[[str, list[str]], bool]
    resolve_pin_spelling: Callable[[str, list[str]], str]
    to_provider_id: Callable[[str, str], str]
    to_acp_id: Callable[[str], str]
    inc_session_created: Callable[[], None]
    get_sel: Callable[[], Any]
    get_subprocess_executor: Callable[[], Executor]
    get_sync_kill_provider: Callable[[], Callable[[LLMProvider], None]]
    agents_dir_path: Callable[[], Path]
    resolve_runtime_agent: Callable[..., str]
    read_agent_spec: Callable[..., dict[str, Any] | None]
    spec_model: Callable[[dict[str, Any]], str]
    agent_model_cache: Callable[[], dict[str, tuple[str, float, float]]]


def cwd_moved_for_reuse(raw_bound: object, cwd: str | None, requested_default: str | None) -> bool:
    """Whether a claim's directory disagrees with the one a live session is bound to.

    Extracted so the comparison can be driven directly: it runs on EVERY reuse, and its
    failure mode is silent -- a false move evicts a warm session, so the slot cold-starts
    each turn or exhausts its retry budget with nothing raised.

    Both sides go through ``resolved_cwd``, and that symmetry is the point. Only one side
    normalized makes the answer depend on which side happened through ``Path``: a provider
    reporting a raw string disagrees with a caller's normalized one, and on Windows ``Path``
    rewrites separators, so two spellings of one directory compare unequal and every reuse
    evicts. Deliberately NOT realpath -- that is a filesystem call under the registry lock,
    and the binding is the spelling the session was OPENED with, which is what a claim
    restates, so a symlinked root compares equal to itself without resolving it.
    """
    # A provider tracking no real directory STRING reports nothing to disagree with, which
    # is not the same as reporting no directory. The ABC default is "".
    bound_readable = isinstance(raw_bound, str) and raw_bound != ""
    bound_cwd, requested_cwd = normalized_reuse_cwds(raw_bound, cwd, requested_default)
    # `None` states no requirement; `""` states "the default workspace".
    return cwd is not None and bound_readable and requested_cwd != bound_cwd


def normalized_reuse_cwds(
    raw_bound: object, cwd: str | None, requested_default: str | None
) -> tuple[str, str]:
    """The two spellings the reuse comparison is made on, as ``(bound, requested)``.

    One definition, because the claim gate needs the PAIR as well as the verdict: the arm
    agreement compares against these values rather than against the boolean, and a second
    copy of the normalization beside the call would drift from this one silently.
    """
    bound_cwd = resolved_cwd(raw_bound) if isinstance(raw_bound, str) and raw_bound else ""
    requested_cwd = (
        "" if cwd is None else (requested_default if cwd == "" else resolved_cwd(cwd))
    ) or ""
    return bound_cwd, requested_cwd


@dataclass(slots=True)
class RetireArm:
    """One key's retirement arm, and the generation that outlives it.

    The three facts share a key and NOT a lifetime: spending an arm drops the directory and the
    agent it names, while the generation must survive, because a start compares itself against
    that counter to learn it is stale. Holding them in one record makes that asymmetry a single
    method rather than an invariant every caller has to remember.

    ``cwd`` is already through ``resolved_cwd``, so a cleared project is the concrete
    default-workspace path and never an empty string. ``None`` means the arm states NO
    directory, which is not the same as stating the default -- see ``_record_arm_cwd``.

    ``requires_sid_clear`` is provenance a directory cannot carry. A discard bumps the
    generation without moving the project, so an arm can hold a resolved cwd from an EARLIER
    project change while naming a conversation that was thrown away. A retry reading only the
    directory then states a real path, which is not ``CWD_CLEARED``, so the resume guard does
    not fire and the discarded conversation comes back. Dropped by ``spend`` with the other
    per-episode facts: the next conversation has its own SID, and a flag that outlived its
    episode would wipe that one instead.
    """

    generation: int = 0
    cwd: str | None = None
    agent: str | None = None
    requires_sid_clear: bool = False

    def spend(self) -> None:
        """Drop what the arm names. The generation is deliberately untouched."""
        self.cwd = None
        self.agent = None
        self.requires_sid_clear = False


@dataclass(slots=True)
class SessionRegistryState:
    """Mutable state exclusively owned by the allocation boundary."""

    sessions: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    start_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    starting_pids: set[int] = field(default_factory=set)
    allocation_reservations: dict[str, set[object]] = field(default_factory=dict)
    ownership_generations: dict[str, int] = field(default_factory=dict)
    subagent_runtimes: dict[str, Any] = field(default_factory=dict)
    subagent_runtime_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    continuable_keys: set[str] = field(default_factory=set)
    continuable_fallback: Callable[[str], bool] | None = None
    # Keys whose next claim must NOT be served a reused session, armed when a
    # teardown was refused. Keyed by string rather than by session object so it
    # still covers a cold start that has not registered yet.
    retire_arms: dict[str, RetireArm] = field(default_factory=dict)


class _AllocationOwner(Protocol):
    """Facade and cross-service surface consumed by allocation."""

    _cfg: Any
    _provider_factory: ProviderFactory | None
    _session_map: Any
    _recycling: dict[str, Any]
    _pool_size: int
    _pool_agent: str
    _pool_cwd: str
    _warm_pool: asyncio.Queue[tuple[LLMProvider, float]]
    _background_tasks: set[asyncio.Task[Any]]
    _bg_runtime: Any | None

    def _fold_key(self, key: str) -> str: ...

    def get_provider(self, key: str) -> LLMProvider | None: ...

    async def get_subagent_runtime(
        self, parent_session_key: str, agent: str | None = None
    ) -> Any: ...

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
    ) -> Any: ...

    async def _reacquire_and_validate(
        self,
        key: str,
        session: Any,
        *,
        wait_if_busy: bool = True,
        cwd: str | None = None,
    ) -> bool: ...

    async def _evict_stale_session(self, key: str, session: Any) -> None: ...

    async def open_task_session(
        self, parent_session_key: str, session_key: str, **kwargs: Any
    ) -> Any: ...

    def _get_session_agent(self, session_key: str) -> str: ...

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict[str, Any]: ...

    async def _drain_and_claim(self, agent: str | None) -> LLMProvider | None: ...

    def _record_pool_decision(self, decision: str, key: str) -> None: ...

    def _schedule_replenish(self) -> None: ...

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None: ...

    def _resolve_agent_model(self, agent: str) -> str: ...

    def _ensure_cleanup_task(self) -> None: ...

    async def get_or_create(self, key: str, **kwargs: Any) -> Any: ...

    async def reset(self, key: str, **kwargs: Any) -> bool: ...

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None: ...

    def mark_continuable(self, key: str) -> None: ...

    def _is_continuable_key(self, folded: str) -> bool: ...

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None: ...


def _collect_parent_runtime_kwargs(
    owner: _AllocationOwner,
    parent_session_key: str,
) -> dict[str, Any]:
    """Mirror the parent client's sandbox, gateway, env, and backend posture."""
    provider = owner.get_provider(parent_session_key)
    if provider is None:
        return {}
    client = getattr(provider, "client", None) or getattr(provider, "_client", None)
    if client is None:
        return {}
    kwargs: dict[str, Any] = {}
    for attribute, key in (
        ("_sandbox_mode", "sandbox_mode"),
        ("_extra_env", "extra_env"),
        ("_mcp_gateway_overlay", "mcp_gateway_overlay"),
        ("_mcp_gateway_socket", "mcp_gateway_socket"),
        ("backend", "acp_backend"),
    ):
        value = getattr(client, attribute, None)
        if value is not None:
            kwargs[key] = value
    return kwargs


class SessionAllocationService:
    """Allocate providers and serialize claims while the manager stays facade."""

    def __init__(
        self,
        owner: _AllocationOwner,
        deps: AllocationDeps,
        *,
        state: SessionRegistryState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state

    # Compatibility properties preserve identity for maps, locks, and sets.
    @property
    def _sessions(self) -> dict[str, Any]:
        return self.state.sessions

    @_sessions.setter
    def _sessions(self, value: dict[str, Any]) -> None:
        self.state.sessions = value

    @property
    def _lock(self) -> asyncio.Lock:
        return self.state.lock

    @_lock.setter
    def _lock(self, value: asyncio.Lock) -> None:
        self.state.lock = value

    @property
    def _closing(self) -> bool:
        return self.state.closing

    @_closing.setter
    def _closing(self, value: bool) -> None:
        self.state.closing = value

    @property
    def _start_sem(self) -> asyncio.Semaphore:
        return self.state.start_sem

    @_start_sem.setter
    def _start_sem(self, value: asyncio.Semaphore) -> None:
        self.state.start_sem = value

    @property
    def _starting_pids(self) -> set[int]:
        return self.state.starting_pids

    @_starting_pids.setter
    def _starting_pids(self, value: set[int]) -> None:
        self.state.starting_pids = value

    @property
    def _allocation_reservations(self) -> dict[str, set[object]]:
        return self.state.allocation_reservations

    @_allocation_reservations.setter
    def _allocation_reservations(self, value: dict[str, set[object]]) -> None:
        self.state.allocation_reservations = value

    @property
    def _ownership_generations(self) -> dict[str, int]:
        return self.state.ownership_generations

    @_ownership_generations.setter
    def _ownership_generations(self, value: dict[str, int]) -> None:
        self.state.ownership_generations = value

    @property
    def _subagent_runtimes(self) -> dict[str, Any]:
        return self.state.subagent_runtimes

    @_subagent_runtimes.setter
    def _subagent_runtimes(self, value: dict[str, Any]) -> None:
        self.state.subagent_runtimes = value

    @property
    def _subagent_runtime_locks(self) -> dict[str, asyncio.Lock]:
        return self.state.subagent_runtime_locks

    @_subagent_runtime_locks.setter
    def _subagent_runtime_locks(self, value: dict[str, asyncio.Lock]) -> None:
        self.state.subagent_runtime_locks = value

    @property
    def _continuable_keys(self) -> set[str]:
        return self.state.continuable_keys

    @_continuable_keys.setter
    def _continuable_keys(self, value: set[str]) -> None:
        self.state.continuable_keys = value

    def _arm(self, folded: str) -> RetireArm:
        """This key's record, created empty if absent. For a WRITE."""
        return self.state.retire_arms.setdefault(folded, RetireArm())

    def _arm_if_any(self, folded: str) -> RetireArm | None:
        """This key's record, or ``None``. For a READ, which must not create one."""
        return self.state.retire_arms.get(folded)

    def _generation(self, folded: str) -> int:
        """This key's monotonic counter, bumped every time its project changes. Zero if absent.

        Staleness is an ORDERING question -- did this provider start before the change
        it must honour -- and the directory a caller states cannot answer it: a cold
        start begun before the change and a slot recreated under the same name after it
        both state something other than the armed target. Comparing the generation the
        provider started at against the current one separates them, so a recreated slot
        keeps its own project instead of inheriting the previous slot's.

        A read never creates a record, so a key that was never armed reports zero rather
        than gaining an entry that ``discard_all_retire_arms`` would then have to clear.
        """
        arm = self._arm_if_any(folded)
        return arm.generation if arm is not None else 0

    @property
    def _continuable_fallback(self) -> Callable[[str], bool] | None:
        return self.state.continuable_fallback

    @_continuable_fallback.setter
    def _continuable_fallback(self, value: Callable[[str], bool] | None) -> None:
        self.state.continuable_fallback = value

    def _fold_key(self, key: str) -> str:
        """Resolve exact, canonical, then legacy aliases across live and reserved keys."""

        def owned(candidate: str) -> bool:
            return candidate in self._sessions or bool(self._allocation_reservations.get(candidate))

        if owned(key):
            return key
        canonical = self._deps.canonical_key(key)
        if canonical != key and owned(canonical):
            return canonical
        bare = self._deps.legacy_key(key)
        if bare is not None and owned(bare):
            return bare
        return key

    def has_session(self, key: str) -> bool:
        return self._owner._fold_key(key) in self._sessions

    def spend_retire_arm(self, key: str) -> None:
        """Drop *key*'s retirement arm, for a teardown that ENDS the slot generation.

        Called only where the slot itself is gone -- ``destroy``, which deletes the
        session-map entry. The arm names a directory a SUCCESSOR must bind, so once no
        successor of that slot can arrive the only claim left to apply it to is a
        different slot recreated under the same name, which would silently inherit the
        previous project.

        Cleanup that keeps the slot (``remove``, ``remove_if_unclaimed``) deliberately
        calls nothing: the arm is still owed to a real claim, and it doubles as the retry
        target for a start the cleanup evicts, whose own frame carries only the
        pre-change directory.

        The key's GENERATION is never dropped here. That counter is what refuses a start
        still in flight, and a teardown it must survive is exactly the case where the arm
        itself has to go.
        """
        folded = self._owner._fold_key(key)
        if (arm := self._arm_if_any(folded)) is not None:
            arm.spend()

    def supersede_arm_for_new_slot(self, key: str, *, only_generation: int | None = None) -> None:
        """Release *key*'s arm because the slot that owned it is gone for good.

        Two seams call this, and neither teardown verb can: ``destroy`` spends the arm, but
        the close and sweep paths run ``remove``, which preserves it deliberately -- it is
        still owed to a start the cleanup evicts, whose own frame carries only the pre-change
        directory. So the arm legitimately outlives the SESSION while the SLOT is gone.

        Slot MINT is the first seam: a freshly minted slot has never been armed, so any arm
        on its key belongs to a previous occupant, whose relative writes would otherwise land
        in that occupant's project. Slot REUSE returns before this is reached, keeping the
        retry target ``remove`` preserves. FINAL slot teardown is the second: without it a
        slot closed and never recreated left both PENDING entries resident until process
        exit, so on a long-lived gateway every transient key that saw a change accumulated.

        Reclaims the two pending maps only. The GENERATION is bumped and RETAINED, never
        dropped: clearing would let a start still in flight from the previous occupant
        compare equal to a fresh key's zero and bind here. One integer per key the process
        has armed therefore stays resident, which is the cost of that guard.

        ``only_generation`` narrows this to a RETRACTION of one specific arm, for a producer
        unwinding its own work. The seams above pass nothing, because a slot that is gone
        invalidates every arm on its key regardless of who wrote it. A producer that armed
        and then had to unwind passes the generation
        :meth:`mark_retire_on_next_claim` returned it; if the resident generation has moved
        on, another producer armed this key inside the unwinding request's await window, so
        the arm named here is already superseded. That case RETURNS EARLY: nothing is dropped
        and the generation is left alone, because bumping it would invalidate the other
        producer's in-flight start -- the same cross-project write this scoping prevents.

        Whenever the call does proceed -- every unscoped caller, and a retraction whose
        generation still matches -- it BUMPS the generation before spending the arm. On the
        matched path the resident generation is this producer's own, so there is no other
        producer's start to invalidate, and the bump is what makes this producer's own
        in-flight start read as stale at registration instead of being served.
        """
        folded = self._owner._fold_key(key)
        if only_generation is not None and self._generation(folded) != only_generation:
            return
        arm = self._arm(folded)
        arm.generation += 1
        arm.spend()

    def discard_all_retire_arms(self) -> None:
        """Drop every arm and generation, for a shutdown that retires all keys at once."""
        self.state.retire_arms.clear()

    def note_conversation_discarded(self, key: str) -> None:
        """Bump this key's generation because its conversation was thrown away.

        The clear path treats "nothing registered" as already-cleared and answers True, but
        a cold start in flight has CACHED its resume SID and not registered yet, so clearing
        the persisted map does not reach it: it registers afterwards carrying the very
        conversation the caller was told was gone. The generation is what a start compares
        itself against at registration, so bumping it here is what makes that late arrival
        read as stale and retire instead of being served.

        Arms no cwd, unlike ``note_project_change``: a clear moves no directory, so the
        binding a retry must honour is unchanged and recording one would misdirect it. It does
        record that the SID must go, which the directory cannot express -- an arm may still
        hold a resolved cwd from an earlier project change, and a retry that reads only the
        directory states a real path rather than ``CWD_CLEARED``, so the resume guard does not
        fire and the discarded conversation is served again.
        """
        folded = self._owner._fold_key(key)
        arm = self._arm(folded)
        arm.generation += 1
        arm.requires_sid_clear = True

    def _record_arm_cwd(self, folded: str, cwd: str | None) -> None:
        """Record the directory an arm states, or that it states NONE.

        ``None`` is not ``CWD_CLEARED``. A never-scoped slot's claim states no directory, so
        the warm pool and its stored-cwd resume override still apply; resolving ``None`` here
        would arm the per-session default that claim deliberately does not ask for, and the
        next turn's relative writes would land outside the directory it was resuming. The
        entry is DROPPED rather than left, so an earlier change's directory cannot outlive
        the generation bump that supersedes it.
        """
        if cwd is None:
            self._arm(folded).cwd = None
            return
        self._arm(folded).cwd = resolved_cwd(cwd, folded)

    def note_project_change(self, key: str, cwd: str | None) -> None:
        """Record that this key's project moved TO ``cwd``, without arming a refusal.

        Not every project change raises an arm: the agent and workspace switches commit a
        new project and tear the session down directly. The generation bump alone is not
        enough, because a start already in flight is then evicted and RETRIED -- and the
        retry re-reads the cwd its own frame was called with, which is the pre-switch one.
        So the committed directory is recorded with the generation and becomes what the
        retry binds. Without it a generation-only switch evicts a provider and replaces it
        with another bound to the project the user just left.

        Distinct from ``mark_retire_on_next_claim`` only in not flagging a REGISTERED
        session for retirement: these callers tear their session down themselves.
        """
        folded = self._owner._fold_key(key)
        generation = self._generation(folded) + 1
        self._arm(folded).generation = generation
        self._record_arm_cwd(folded, cwd)

    def mark_retire_on_next_claim(
        self, key: str, cwd: str | None, *, agent: str | None = None
    ) -> int:
        """Mark this key's session invalid for reuse without touching a running turn.

        For a teardown that had to be REFUSED. The refusal keeps a streaming reply
        alive, but the reason for the teardown does not go away, so the next claim
        must not be handed that session either.

        Records the KEY, not just the object, because "no session registered" is NOT
        the same as "nothing to protect against": a cold start holds no registry entry
        until it finishes, so a probe during that window sees nothing while a provider
        bound to the pre-change directory is already on its way. Pinning only a
        registered object would miss exactly that case. `_reacquire_and_validate`
        consumes the key, so a session that registers AFTER this call is refused on
        its next claim just the same.

        What this can and cannot promise: a turn already streaming keeps its provider
        (that is what the refusal is FOR, and tearing it down mid-reply is the harm
        `skip_if_busy` exists to prevent), so the guarantee is that no LATER turn is
        served the stale session -- not that an in-flight one is retro-corrected.

        `retire_on_identity_change` is the flag `session_lifecycle` already sets when
        its own teardown finds the session locked, so the registered case reuses that
        path rather than adding a second one.

        Returns the GENERATION recorded here. That is what lets a producer retract its
        OWN arm and nothing else: it passes the value back to
        :meth:`supersede_arm_for_new_slot`, whose drop then no-ops once another producer has
        armed the same key in between. Whether a session happened to be registered is still
        unreported -- the arm covers both shapes -- so that would be a value with no reader.
        """
        folded = self._owner._fold_key(key)
        # The generation is bumped and the pending maps OVERWRITTEN, so a LATER change
        # to the same key supersedes this arm rather than leaving two live answers.
        generation = self._generation(folded) + 1
        self._arm(folded).generation = generation
        self._record_arm_cwd(folded, cwd)
        if agent is not None:
            self._arm(folded).agent = agent or "kirocrew"
        session = self._sessions.get(folded)
        if session is not None:
            session.retire_on_identity_change = True
        return generation

    def transfer_retire_arm(self, from_key: str, to_key: str, cwd: str | None) -> None:
        """Re-point an arm from an ABANDONED key onto the one the slot now runs on.

        For a slot that REBOUND between arming and consuming. Arming the live key alone
        would leave the abandoned key's arm in the map, where nothing drops it: an arm is
        cleared by the claim that satisfies it, and no claim arrives for a key the slot
        left. The map is keyed by STRING, so a later session under that same key reads an
        arm naming a directory chosen for a binding that is gone.

        Distinct from :meth:`spend_retire_arm`, which is for a teardown that ENDS the slot
        generation and so drops an arm no successor is owed; here the arm is still owed and
        only its ADDRESS was wrong. One synchronous step, so the requirement is never
        recorded twice or not at all. The agent travels with it because only the arm states
        which agent a successor must run. ``from_key``'s GENERATION stays, for the reason
        :meth:`spend_retire_arm` keeps its own -- see the rebind producer in
        ``docs/system-specs/modules/session.md``.

        An EQUIVALENT same-key re-arm -- same folded key, already holding the same resolved
        target -- keeps the generation rather than bumping it. The deferred-reset retry
        re-enters every few seconds while sub-agents stay attached, and each bump rejects a
        cold start still resolving its own model, so a slow start is refused on every pass
        until it gives up. Equivalence is deliberately narrow: a DIFFERENT target on the
        same key is a genuine re-arm and must supersede. The agent needs no comparison in
        that case because ``carried_agent`` is read from this very key, so it cannot differ.
        """
        source = self._owner._fold_key(from_key)
        target = self._owner._fold_key(to_key)
        source_arm = self._arm_if_any(source)
        carried_agent = source_arm.agent if source_arm is not None else None
        target_arm = self._arm_if_any(target)
        target_cwd = target_arm.cwd if target_arm is not None else None
        if source == target and target_cwd == (
            resolved_cwd(cwd, target) if cwd is not None else None
        ):
            # The retry loop re-arms the SAME key with the SAME target every few seconds.
            # Bumping the generation there rejects an in-flight cold start on every pass.
            session = self._sessions.get(target)
            if session is not None:
                session.retire_on_identity_change = True
            return
        self.mark_retire_on_next_claim(to_key, cwd, agent=carried_agent)
        if source != target:
            if source_arm is not None:
                source_arm.spend()

    def get_provider(self, key: str) -> LLMProvider | None:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.provider if session else None

    def _generation_key(self, key: str) -> str:
        """Stable generation bucket shared by canonical and legacy Slack aliases."""
        return self._deps.canonical_key(key)

    def advance_ownership_generation(self, key: str) -> int:
        """Advance and return *key*'s monotonic ownership generation."""
        bucket = self._generation_key(key)
        generation = self._ownership_generations.get(bucket, 0) + 1
        self._ownership_generations[bucket] = generation
        return generation

    def session_generation(self, key: str) -> int:
        """Return the monotonic logical-key ownership generation.

        Zero is the first absence generation, not a reusable sentinel: every
        reservation publication/removal advances the canonical key's counter,
        so an absent -> successor -> absent ABA cannot match a stale capture.
        """
        return self._ownership_generations.get(self._generation_key(key), 0)

    def session_keys(self) -> frozenset[str]:
        """Snapshot live and in-flight registry keys on the event-loop thread."""
        reserved = {
            key for key, reservations in self._allocation_reservations.items() if reservations
        }
        return frozenset(self._sessions.keys() | reserved)

    def has_allocation_reservation(self, key: str) -> bool:
        """Return whether a folded alias has an allocation/claim in flight."""
        return bool(self._allocation_reservations.get(self._owner._fold_key(key)))

    async def try_acquire(self, key: str) -> bool:
        """Acquire only an exact-key idle session; alias folding is intentional absent."""
        session = self._sessions.get(key)
        if session is None or session.semaphore.locked():
            return False
        # Idle Semaphore(1).acquire completes without suspending, keeping the
        # locked check and decrement atomic on the event loop.
        await session.semaphore.acquire()
        return True

    def active_providers(self) -> list[LLMProvider]:
        return [session.provider for session in self._sessions.values()]

    def any_active_turn(self) -> bool:
        return any(
            self._deps.provider_has_active_turn(session.provider)
            for session in self._sessions.values()
        )

    def get_pid(self, key: str) -> int | None:
        session = self._sessions.get(self._owner._fold_key(key))
        if not session:
            return None
        try:
            return session.provider.client._pid
        except AttributeError:
            return None

    async def get_subagent_runtime(self, parent_session_key: str, agent: str | None = None) -> Any:
        """Get or spawn the canonical shared companion runtime for a parent."""
        if await asyncio.to_thread(private_memory_store_for_session, parent_session_key):
            raise RuntimeError("Private member memory requires a dedicated runtime")
        runtime_type, runtime_dead = self._deps.runtime_types()
        max_retries = 1
        attempt = 0
        selected_agent = agent
        while True:
            lock = self._subagent_runtime_locks.setdefault(parent_session_key, asyncio.Lock())
            async with lock:
                if self._subagent_runtime_locks.get(parent_session_key) is not lock:
                    # release_subagent_runtime removed the lock while we waited;
                    # retry under the newly-canonical lock without spending a
                    # process-spawn retry.
                    continue
                existing = self._subagent_runtimes.get(parent_session_key)
                if existing is not None and existing.is_alive():
                    return existing
                if existing is not None:
                    try:
                        await existing.kill()
                    except Exception:
                        self._deps.logger.debug(
                            "get_subagent_runtime: dead runtime kill failed for %s",
                            parent_session_key,
                            exc_info=True,
                        )
                selected_agent = (
                    selected_agent
                    or self._owner._get_session_agent(parent_session_key)
                    or "kirocrew"
                )
                kwargs = self._owner._parent_runtime_kwargs(parent_session_key)
                runtime = runtime_type(agent=selected_agent, **kwargs)
                try:
                    await runtime.spawn()
                except runtime_dead:
                    if attempt >= max_retries:
                        raise
                    attempt += 1
                    self._deps.logger.warning(
                        "Subagent runtime spawn failed for %s (attempt %d/%d), retrying",
                        parent_session_key,
                        attempt,
                        max_retries + 1,
                        exc_info=True,
                    )
                    continue
                self._subagent_runtimes[parent_session_key] = runtime
                return runtime

    async def release_subagent_runtime(self, parent_session_key: str) -> None:
        """Serialize release with spawn and kill the detached runtime off-map."""
        lock = self._subagent_runtime_locks.get(parent_session_key)
        if lock is not None:
            async with lock:
                runtime = self._subagent_runtimes.pop(parent_session_key, None)
                # A waiter on this removed lock re-checks canonical identity in
                # get_subagent_runtime and retries under the live lock.
                self._subagent_runtime_locks.pop(parent_session_key, None)
        else:
            runtime = self._subagent_runtimes.pop(parent_session_key, None)
        if runtime is not None:
            try:
                await runtime.kill(expected=True)
            except Exception:
                self._deps.logger.warning(
                    "Failed to kill subagent runtime for %s",
                    parent_session_key,
                    exc_info=True,
                )

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
    ) -> Any:
        """Adopt a configured bootstrap provider's runtime for a task run."""
        if await asyncio.to_thread(private_memory_store_for_session, parent_session_key):
            raise RuntimeError("Private member memory requires a dedicated runtime")
        owner = self._owner
        if not owner._provider_factory:
            # Outside the per-key lock: get_subagent_runtime takes that lock and
            # asyncio.Lock is not reentrant.
            return await owner.get_subagent_runtime(parent_session_key, agent=agent)

        if parent_session_key not in self._subagent_runtime_locks:
            self._subagent_runtime_locks[parent_session_key] = asyncio.Lock()
        lock = self._subagent_runtime_locks[parent_session_key]
        async with lock:
            existing = self._subagent_runtimes.get(parent_session_key)
            if existing is not None and existing.is_alive():
                return existing
            provider = owner._provider_factory(parent_session_key, agent=agent, cwd=cwd)
            await provider.start()
            session_provider = getattr(provider, "_client", None)
            runtime = getattr(session_provider, "_runtime", None)
            if session_provider is not None and runtime is not None:
                try:
                    session_provider._owns_runtime = False
                except Exception:
                    self._deps.logger.debug("run runtime ownership transfer failed", exc_info=True)
                self._subagent_runtimes[parent_session_key] = runtime
                try:
                    handle = getattr(session_provider, "_handle", None)
                    session_id = getattr(handle, "session_id", None) or getattr(
                        handle, "_session_id", None
                    )
                    if session_id:
                        await runtime.terminate_session(session_id)
                except Exception:
                    self._deps.logger.debug(
                        "run runtime bootstrap-session terminate failed", exc_info=True
                    )
                return runtime
            try:
                await provider.shutdown()
            except Exception:
                self._deps.logger.debug(
                    "run runtime bootstrap provider shutdown failed", exc_info=True
                )
        return await owner.get_subagent_runtime(parent_session_key, agent=agent)

    async def _reacquire_and_validate(
        self,
        key: str,
        session: Any,
        *,
        wait_if_busy: bool = True,
        cwd: str | None = None,
    ) -> bool:
        """Acquire with the global lock released, then validate exact identity.

        ``cwd`` also validates the session's BOUND directory. It is applied when a
        provider is CREATED and never re-applied, so a live session whose project has
        since changed would otherwise be handed back bound to the OLD directory and
        the turn's relative writes would land there.

        Checked HERE rather than at the reuse decision because this runs with the
        semaphore HELD: any turn that was streaming on this provider has finished, so
        returning False -- which sends the caller through ``_evict_stale_session`` --
        cannot tear a live reply down mid-stream. The reuse decision runs before the
        semaphore is claimed, where that guarantee does not hold.

        Validating it here also covers every reuse path in this class rather than only
        the callers that remember to ask: all four claims -- both in ``get_or_create``
        and both in ``open_task_session`` -- pass their own ``cwd`` through this one
        helper. A caller that passes none states no requirement and cannot mismatch.

        Guarded on BOTH being set, mirroring the pool gate's own comparison: a caller
        passing no ``cwd`` states no requirement and must not evict a session serving
        others correctly. The isinstance check is that rule applied to the other side
        -- a provider tracking no real directory string reports no binding to
        disagree with, and evicting on an unreadable one would churn every reuse
        rather than protect anything.
        """
        if not wait_if_busy and session.semaphore.locked():
            raise SessionBusyError(key)
        # Resolved off-thread BEFORE the lock: the cleared case stats and realpaths the
        # workspace root, and synchronous I/O with the registry held wedges every session.
        requested_default = await asyncio.to_thread(resolved_cwd, cwd, key) if cwd == "" else None
        # The armed alias resolves through a CONFIG LOAD, which on a cache miss reads and
        # schema-validates -- offloaded here because the decision window below takes no await.
        pre_armed = self._arm_if_any(key)
        pre_armed_alias = pre_armed.agent if pre_armed is not None else None
        pre_armed_target = (
            await asyncio.to_thread(self._deps.resolve_runtime_agent, pre_armed_alias, None)
            if pre_armed_alias
            else None
        )
        # Re-checked AFTER the awaits above: two suspension points separate the first check
        # from the acquire, so a caller that asked never to block would block here.
        if not wait_if_busy and session.semaphore.locked():
            raise SessionBusyError(key)
        # An idle Semaphore(1) acquires without suspension, so with the re-check directly
        # above this is the authoritative non-waiting claim boundary.
        await session.semaphore.acquire()
        cwd_moved = False
        try:
            async with self._lock:
                raw_bound = getattr(session.provider, "cwd", None)
                bound_readable = isinstance(raw_bound, str) and raw_bound != ""
                # The same normalization the comparison makes, from ONE definition: the arm
                # agreement below compares against these spellings, not against the verdict.
                bound_cwd, requested_cwd = normalized_reuse_cwds(raw_bound, cwd, requested_default)
                # One definition, driven directly by its own tests: the symmetry this
                # comparison depends on is invisible at the call site.
                cwd_moved = cwd_moved_for_reuse(raw_bound, cwd, requested_default)
                cwd_stated = cwd is not None
                reg_arm = self._arm_if_any(key)
                armed_target = reg_arm.cwd if reg_arm is not None else None
                armed_agent = reg_arm.agent if reg_arm is not None else None
                retire_armed = armed_target is not None or armed_agent is not None
                # Only the REGISTERED session interacts with the arm: a claimant holding a
                # session already replaced would otherwise spend it for the successor.
                is_registered = self._sessions.get(key) is session
                cwd_satisfied = (
                    is_registered
                    and bound_readable
                    and (
                        (
                            cwd_stated
                            and requested_cwd == bound_cwd
                            and (armed_target is None or bound_cwd == armed_target)
                        )
                        # A cwd-LESS claim cannot state agreement, so its BINDING settles it.
                        # An arm with no directory states no requirement to satisfy.
                        or (not cwd_stated and (armed_target is None or bound_cwd == armed_target))
                    )
                )
                # A SEPARATE question from the directory, so it needs its own answer: a
                # project-scope switch keeps the directory. See session.md.
                # The arm names an ALIAS and the session records the RESOLVED agent, so the
                # target is the value pre-resolved off-thread, used only while the arm holds it.
                armed_target_agent = pre_armed_target if pre_armed_alias == armed_agent else None
                agent_satisfied = armed_agent is None or (session.agent or "kirocrew") in (
                    armed_agent,
                    armed_target_agent or armed_agent,
                )
                retire_applies = retire_armed and not (cwd_satisfied and agent_satisfied)
                if retire_applies:
                    # This frame can refuse WITHOUT evicting, so a claim racing the
                    # registration would find the arm gone and reuse the stale provider.
                    session.retire_on_identity_change = True
                claim_answers_arm = cwd_satisfied and agent_satisfied
                still_valid = (
                    self._sessions.get(key) is session
                    and not session.retire_on_identity_change
                    and not retire_applies
                    and self._deps.provider_effectively_alive(session.provider)
                    and not cwd_moved
                )
                if claim_answers_arm and still_valid:
                    # Spent on ACCEPTANCE, not on satisfaction: a rejected claim (dead
                    # provider, moved key) must leave the arm for its replacement to pay.
                    if reg_arm is not None:
                        reg_arm.spend()
        except BaseException:
            # The held-semaphore contract was never returned to the caller.
            session.semaphore.release()
            raise
        if not still_valid:
            try:
                if cwd_moved:
                    # Tear down BEFORE the permit is released, and ONLY for a moved
                    # directory. Releasing first leaves the session still registered
                    # with a FREE permit, so another acquirer can win it and be
                    # mid-command when the eviction shuts its provider down. Only this
                    # reason exposes that window: it is the one invalidity that fires
                    # on a LIVE, registered, otherwise-usable provider, whereas a
                    # session whose identity already moved is not the registry
                    # occupant and a dead process has nothing to hand out. Popping
                    # under the permit means a racing acquirer finds no entry and
                    # cold-starts instead.
                    #
                    # Scoped rather than unconditional because the other reasons have
                    # callers that deliberately do NOT evict -- `recycle_background`
                    # returns and leaves the entry in place, since tearing it down
                    # there would kill a session another path already owns.
                    await self._evict_stale_session(key, session)
            finally:
                # In a `finally` so a cancellation while the eviction awaits the
                # registry lock cannot leave this permit held: that would wedge the
                # key for every later turn, which is worse than any window it closes.
                # Safe to release even if the eviction was interrupted before popping,
                # because the check is IDEMPOTENT -- the directory still does not
                # match, so the next claim re-detects it and evicts again. NOT
                # `asyncio.shield`: shielding would let the eviction keep running
                # while this frame released, which is exactly the release-before-pop
                # ordering the branch above exists to prevent.
                session.semaphore.release()
        return still_valid

    async def _evict_stale_session(self, key: str, session: Any) -> None:
        """Pop only the observed stale object and close it outside the lock."""
        dead: LLMProvider | None = None
        async with self._lock:
            if self._sessions.get(key) is session:
                del self._sessions[key]
                self.advance_ownership_generation(key)
                dead = session.provider
                # The arm is NOT spent here: eviction is not acceptance, and a successor
                # can register under it and die before serving. Only a live claim spends it.
                # Same tick as the removal. Left unrecorded, the start crumb
                # survives and the next boot calls this a crash.
                await record_session_ended(key, end_reason=END_REASON_EVICTED)
        if dead is not None:
            await asyncio.to_thread(self._deps.unlink_session_queue, session)
            try:
                await dead.shutdown()
            except Exception:
                self._deps.logger.warning(
                    "Failed to shut down stale provider for %s", key, exc_info=True
                )

    async def open_task_session(
        self,
        parent_session_key: str,
        session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
        approval_policy: str = "",
        _won_race_retries: int = 0,
    ) -> tuple[LLMProvider, bool, bool]:
        """Open a per-step session on the task run's shared runtime.

        A descriptor-bound macOS runtime cannot safely serve a later exact cwd
        through ACP's string-only session request. In that case the facade's
        normal dedicated-provider path binds a runtime at the requested cwd.
        """
        # Circular import: runtime imports the session provider path indirectly.
        from kiro_crew.acp.runtime import AcpWorkspaceBindingError

        owner = self._owner
        key = owner._fold_key(session_key)
        private_stores = await asyncio.gather(
            asyncio.to_thread(private_memory_store_for_session, key),
            asyncio.to_thread(private_memory_store_for_session, parent_session_key),
        )
        if private_stores[1] and not private_stores[0]:
            raise RuntimeError(
                "A private member task requires a trusted child memory binding before execution"
            )
        if any(private_stores):
            return await owner.get_or_create(
                key, agent=agent, approval_policy=approval_policy, cwd=cwd
            )

        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_used = time.monotonic()
                if approval_policy:
                    existing.approval_policy = approval_policy
        if existing is not None:
            if await owner._reacquire_and_validate(key, existing, cwd=cwd):
                return existing.provider, False, False
            await owner._evict_stale_session(key, existing)

        runtime = await owner._get_or_bootstrap_run_runtime(
            parent_session_key, agent=agent, cwd=cwd
        )
        try:
            handle = await runtime.create_session(cwd=cwd or None, agent=agent or None)
        except AcpWorkspaceBindingError:
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
            )
        provider = self._deps.session_provider_type()(handle, runtime)

        duplicate: LLMProvider | None = None
        won_race_session: Any | None = None
        async with self._lock:
            current = self._sessions.get(key)
            if current is not None:
                session = current
                session.last_used = time.monotonic()
                if approval_policy:
                    session.approval_policy = approval_policy
                duplicate = provider
            else:
                session = self._deps.session_factory(
                    provider=provider,
                    first_turn=self._deps.first_turn_fresh,
                    approval_policy=approval_policy,
                    agent=agent or "",
                )
                self._sessions[key] = session
                self.advance_ownership_generation(key)
                won_race_session = session
                try:
                    await record_session_started(key)
                except BaseException:
                    # This await is the only suspension point between registering
                    # the session and returning it. Cancelled here, the caller
                    # hard-kills the provider while the entry stays visible, so a
                    # claimant can be handed a session whose process is already
                    # dying -- and the crumb would outlive it into a false crash.
                    if self._sessions.get(key) is session:
                        del self._sessions[key]
                        self.advance_ownership_generation(key)
                    await discard_session_start(key)
                    raise
        if duplicate is not None:
            try:
                await duplicate.shutdown()
            except Exception:
                self._deps.logger.debug(
                    "open_task_session: duplicate session teardown failed",
                    exc_info=True,
                )
            if await owner._reacquire_and_validate(key, session, cwd=cwd):
                return session.provider, False, False
            await owner._evict_stale_session(key, session)
            maximum = self._deps.constants.won_race_max_retries
            if _won_race_retries >= maximum:
                raise RuntimeError(
                    f"open_task_session({key!r}) exceeded {maximum} won-race "
                    "retries — session kept going stale between acquire and re-validate"
                )
            return await owner.open_task_session(
                parent_session_key,
                session_key,
                agent=agent,
                cwd=cwd,
                approval_policy=approval_policy,
                _won_race_retries=_won_race_retries + 1,
            )
        assert won_race_session is session
        await session.semaphore.acquire()
        return session.provider, True, False

    def _get_session_agent(self, session_key: str) -> str:
        session = self._sessions.get(session_key)
        if session is None:
            return ""
        return getattr(session, "agent", "") or ""

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict[str, Any]:
        return _collect_parent_runtime_kwargs(self._owner, parent_session_key)

    def is_session_sharing_eligible(self, parent_session_key: str) -> bool:
        # Exact-key lookup is current behavior; do not fold this seam here.
        session = self._sessions.get(parent_session_key)
        if session is None:
            return False
        if getattr(session.provider, "_private_memory", False) is True:
            return False
        return getattr(session.provider, "is_session_sharing_eligible", False)

    @staticmethod
    def _runtime_pid(runtime: Any) -> int | None:
        pid = getattr(runtime, "pid", None)
        return pid if isinstance(pid, int) and pid > 0 else None

    def runtime_pids(self) -> list[dict[str, object]]:
        """Return process-identity snapshots without performing OS sampling."""
        rows: list[dict[str, object]] = []
        for key, session in self._sessions.items():
            client = getattr(session.provider, "_client", None)
            if client is None:
                client = session.provider
            runtime = getattr(client, "_runtime", None)
            rows.append(
                {
                    "key": key,
                    "agent": session.agent,
                    "pid": self._runtime_pid(runtime),
                    "owns_runtime": bool(getattr(client, "_owns_runtime", True)),
                    "created_at": session.created_at,
                    "prompts": session.prompt_count,
                }
            )
        self._owner._append_companion_runtime_rows(rows)
        return rows

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None:
        """Append manager-owned background and subagent runtime process rows."""
        now_wall = time.time()
        now_monotonic = time.monotonic()

        def add(label: str, runtime: object, agent: str) -> None:
            try:
                if runtime is None or not runtime.is_alive():  # type: ignore[attr-defined]
                    return
                pid = self._runtime_pid(runtime)
                if pid is None:
                    return
                spawned = getattr(runtime, "_spawn_monotonic", None)
                created = (
                    now_wall - (now_monotonic - spawned)
                    if isinstance(spawned, (int, float))
                    else None
                )
                rows.append(
                    {
                        "key": label,
                        "agent": agent,
                        "pid": pid,
                        "owns_runtime": True,
                        "created_at": created,
                        "prompts": None,
                    }
                )
            except Exception:
                self._deps.logger.debug("runtime_pids: probe failed for %s", label, exc_info=True)

        add(
            "Background runtime",
            self._owner._bg_runtime,
            self._deps.constants.background_agent,
        )
        for parent_key, runtime in list(self._subagent_runtimes.items()):
            add(f"Subagent runtime ({parent_key})", runtime, "")

    def record_success(self, key: str) -> None:
        session = self._sessions.get(self._owner._fold_key(key))
        if session:
            session.consecutive_failures = 0

    async def record_failure(self, key: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        session.consecutive_failures += 1
        if session.consecutive_failures >= self._deps.constants.circuit_breaker_threshold:
            self._deps.logger.error(
                "Circuit breaker tripped for %s (%d consecutive failures) — resetting",
                key,
                session.consecutive_failures,
            )
            await self._owner.reset(key)
            return True
        return False

    def begin_turn(self, key: str) -> None:
        """Yield-free pre-dispatch closing gate for an already-issued lease."""
        if self._closing:
            raise SessionClosingError(
                "SessionManager is closing (gateway restart/shutdown in "
                "progress); refusing to start a turn"
            )

    def mark_continuable(self, key: str) -> None:
        self._continuable_keys.add(self._owner._fold_key(key))

    def unmark_continuable(self, key: str) -> None:
        self._continuable_keys.discard(self._owner._fold_key(key))

    def set_continuable_fallback(self, callback: Callable[[str], bool] | None) -> None:
        self._continuable_fallback = callback

    def _is_continuable_key(self, folded: str) -> bool:
        if folded in self._continuable_keys:
            return True
        fallback = self._continuable_fallback
        if fallback is None:
            return False
        try:
            if fallback(folded):
                self._continuable_keys.add(folded)
                return True
        except Exception:
            self._deps.logger.debug("continuable fallback failed for %s", folded, exc_info=True)
        return False

    def is_continuable(self, key: str) -> bool:
        return self._owner._is_continuable_key(self._owner._fold_key(key))

    # Persistence forwarding deliberately uses the owner's one SessionMap.
    def resumable_sid(self, key: str) -> str | None:
        return self._owner._session_map.get(self._owner._fold_key(key))

    def resumable_hint(self, key: str) -> bool:
        return self._owner._session_map.has_hint(self._owner._fold_key(key))

    def seed_conversation(
        self,
        key: str,
        sid: str,
        *,
        provider: str = "",
        cwd: str = "",
    ) -> None:
        if sid:
            self._owner._session_map.set(
                self._owner._fold_key(key),
                sid,
                provider=provider,
                cwd=cwd,
            )

    def forget_conversation(self, key: str) -> str | None:
        folded = self._owner._fold_key(key)
        sid = self._owner._session_map.get(folded)
        self._owner._session_map.delete(folded)
        self._continuable_keys.discard(folded)
        return sid

    def conversation_provider(self, key: str) -> str:
        return self._owner._session_map.get_provider(self._owner._fold_key(key))

    def release(self, key: str, *, cleanup: bool = False) -> None:
        """Release the current registry occupant's semaphore.

        This intentionally preserves the existing key-only lease identity: it
        does not repair the known stale-release window when a locked replacement
        occupies the same key.
        """
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            if (
                cleanup
                and key.startswith(self._deps.constants.subagent_prefix)
                and not self._owner._is_continuable_key(key)
            ):
                try:
                    session_id = session.provider.session_id
                    if session_id:
                        asyncio.ensure_future(
                            self._owner._safe_cleanup(session.provider, session_id)
                        )
                except Exception:
                    self._deps.logger.debug("Failed to get session_id for cleanup", exc_info=True)
            try:
                session.semaphore.release()
            except ValueError:
                self._deps.logger.warning(
                    "release(%s): session was replaced under us; dropping "
                    "stray semaphore release instead of over-releasing the "
                    "new occupant's",
                    key,
                )

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None:
        try:
            await provider.cleanup_session(session_id)
            self._deps.logger.debug("Cleaned up session files for %s", session_id)
        except Exception:
            self._deps.logger.warning(
                "Failed to clean up session files for %s",
                session_id,
                exc_info=True,
            )

    def is_busy(self, key: str) -> bool:
        session = self._sessions.get(self._owner._fold_key(key))
        return bool(session and session.semaphore.locked())

    def touch(self, key: str) -> bool:
        session = self._sessions.get(self._owner._fold_key(key))
        if session is None:
            return False
        session.last_used = time.monotonic()
        return True

    def enqueue(
        self,
        key: str,
        msg_ts: str,
        text: str,
        *,
        force: bool = False,
        **kwargs: object,
    ) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if force or session.semaphore.locked():
            session.queue.append((msg_ts, text, kwargs))
            return True
        return False

    def dequeue(self, key: str) -> tuple[str, str, dict[str, Any]] | None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return None
        while session.queue:
            msg_ts, text, kwargs = session.queue.popleft()
            if msg_ts not in session.cancelled:
                return msg_ts, text, kwargs
            session.cancelled.discard(msg_ts)
            self._deps.unlink_queued_temp_paths(kwargs)
        return None

    def cancel_queued(self, key: str, msg_ts: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        for index, (queued_ts, _, kwargs) in enumerate(session.queue):
            if queued_ts == msg_ts:
                self._deps.unlink_queued_temp_paths(kwargs)
                del session.queue[index]
                return True
        if session.semaphore.locked():
            session.cancelled.add(msg_ts)
        return False

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if msg_ts in session.cancelled:
            session.cancelled.discard(msg_ts)
            return True
        return False

    def clear_queue(self, key: str) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            for _, _, kwargs in session.queue:
                self._deps.unlink_queued_temp_paths(kwargs)
            session.queue.clear()
            session.cancelled.clear()

    async def is_provider_alive(self, key: str) -> bool | None:
        key = self._owner._fold_key(key)
        async with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return None
        return session.provider.is_process_alive()

    def get_approval_policy(self, key: str) -> str:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.approval_policy if session else ""

    def get_agent(self, key: str) -> str:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.agent if session else ""

    def set_approval_policy(self, key: str, policy: str) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            previous = session.approval_policy
            session.approval_policy = policy
            if previous != policy:
                self._deps.get_sel().log_tool_invocation(
                    session_key=key,
                    source="session",
                    tool_name="set_approval_policy",
                    outcome=policy or "default",
                    metadata={"old_policy": previous, "new_policy": policy},
                )

    def _resolve_agent_model(self, agent: str) -> str:
        """Resolve an agent JSON model with directory-mtime and TTL invalidation."""
        agents_dir = self._deps.agents_dir_path()
        try:
            directory_mtime = agents_dir.stat().st_mtime
        except OSError:
            directory_mtime = 0.0
        now = time.monotonic()
        cache = self._deps.agent_model_cache()

        entry = cache.get(agent)
        if entry is not None:
            cached_model, cached_mtime, cached_at = entry
            if (
                cached_mtime == directory_mtime
                and now - cached_at < self._deps.constants.agent_model_cache_ttl()
            ):
                return cached_model

        model = "auto"
        try:
            for agent_file in agents_dir.glob("*.json"):
                data = self._deps.read_agent_spec(
                    agent_file,
                    operation="resolve_agent_model",
                    source="unknown",
                )
                if data is None:
                    continue
                if data.get("name") == agent or agent_file.stem == agent:
                    model = self._deps.spec_model(data)
                    break
        except Exception:
            pass
        cache[agent] = (model, directory_mtime, now)
        return model

    @staticmethod
    def _is_member_key(key: str) -> bool:
        """Whether *key* addresses a crew member's pinned DM session.

        Wrapper so the pool-bypass arm stays readable and the import stays off
        module top level (circular import: members' module graph is heavy and
        imports config, which sits below this module).
        """
        from kiro_crew.members import is_member_session_key

        return is_member_session_key(key)

    async def _crew_pins_effort(self, agent: str | None, crew_agent: object) -> bool:
        """True when the crew this session runs as pins its own reasoning effort.

        Read off the event loop: ``load_config`` parses (and deep-copies, even on
        a cache hit) the whole config, which is not work to do inline. Only the
        warm-pool decision calls this, so the cost lands once per cold start
        rather than once per turn, and never on a session that was already
        skipping the pool for a cheaper reason.

        Failure answers False -- the pre-field behaviour. An unreadable config
        must not stop a session from starting, and pooling it is only wrong for a
        crew that pins an effort, which is exactly what could not be read.
        """
        try:
            config = await asyncio.to_thread(self._deps.load_config)
            crew = crew_agent if isinstance(crew_agent, str) else None
            return bool(config.crew_pinned_effort(agent, crew))
        except Exception:
            self._deps.logger.warning(
                "Could not read the crew effort pin for agent=%r; pooling as before",
                agent,
                exc_info=True,
            )
            return False

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None:
        """Dispatch blocking provider teardown away from the event-loop thread."""
        kill = self._deps.get_sync_kill_provider()
        try:
            asyncio.get_running_loop().run_in_executor(
                self._deps.get_subprocess_executor(),
                kill,
                provider,
            )
        except RuntimeError:
            # During executor shutdown, a daemon thread is safer than running
            # waitpid/taskkill inline and wedging the event loop.
            threading.Thread(target=kill, args=(provider,), daemon=True).start()

    def _remove_reservation_now(self, key: str, token: object) -> None:
        """Remove a token in the yield-free span after a successful claim."""
        reservations = self._allocation_reservations.get(key)
        if reservations is not None and token in reservations:
            reservations.remove(token)
            self.advance_ownership_generation(key)
            if not reservations:
                self._allocation_reservations.pop(key, None)

    async def _remove_reservation_cancellation_drained(self, key: str, token: object) -> None:
        """Remove one failed/cancelled reservation despite caller cancellation."""

        async def remove() -> None:
            async with self._lock:
                self._remove_reservation_now(key, token)

        cleanup = asyncio.create_task(remove())
        cancellation: asyncio.CancelledError | None = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancellation = exc
        await cleanup
        if cancellation is not None:
            raise cancellation

    async def get_or_create(
        self,
        key: str,
        agent: str | None = None,
        channel_id: str | None = None,
        approval_policy: str = "",
        model: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        speculative: bool = False,
        speculative_resume: bool = False,
        wait_if_busy: bool = True,
        _won_race_retries: int = 0,
        **extra_factory_kwargs: Any,
    ) -> tuple[LLMProvider, bool, bool]:
        """Reserve logical ownership for the complete claim/allocation call."""
        token = object()
        async with self._lock:
            if self._closing:
                raise SessionClosingError(
                    "SessionManager is closing (gateway restart/shutdown in "
                    "progress); refusing to start or resume a turn"
                )
            reserved_key = self._owner._fold_key(key)
            self._allocation_reservations.setdefault(reserved_key, set()).add(token)
            self.advance_ownership_generation(reserved_key)
        try:
            result = await self._get_or_create_impl(
                reserved_key,
                agent=agent,
                channel_id=channel_id,
                approval_policy=approval_policy,
                model=model,
                cwd=cwd,
                extra_env=extra_env,
                speculative=speculative,
                speculative_resume=speculative_resume,
                wait_if_busy=wait_if_busy,
                _won_race_retries=_won_race_retries,
                **extra_factory_kwargs,
            )
        except BaseException:
            await self._remove_reservation_cancellation_drained(reserved_key, token)
            raise
        self._remove_reservation_now(reserved_key, token)
        return result

    async def _get_or_create_impl(
        self,
        key: str,
        agent: str | None = None,
        channel_id: str | None = None,
        approval_policy: str = "",
        model: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        speculative: bool = False,
        speculative_resume: bool = False,
        wait_if_busy: bool = True,
        _won_race_retries: int = 0,
        **extra_factory_kwargs: Any,
    ) -> tuple[LLMProvider, bool, bool]:
        """Claim a live session or cold-start one, returning its held lease.

        The returned tuple is ``(provider, is_new, resumed)``.  A successful
        return always owns the session semaphore and must be paired with
        ``release``.  First-turn observation is consumed only by the real
        claimant that actually wins that semaphore.
        """
        owner = self._owner
        constants = self._deps.constants
        key = owner._fold_key(key)
        # Snapshotted before the FIRST await: everything after is part of this start, so a
        # generation bump landing in there must outrank it.
        started_generation = self._generation(key)
        # A binding can belong to any session kind (cron, delegated run, or
        # consolidation), and must be checked before even reusing a live client.
        private_memory = bool(await asyncio.to_thread(private_memory_store_for_session, key))
        stale_provider: LLMProvider | None = None
        stale_session: Any | None = None
        claimed: Any | None = None
        factory: ProviderFactory
        try:
            async with self._lock:
                if self._closing:
                    raise SessionClosingError(
                        "SessionManager is closing (gateway restart/shutdown in "
                        "progress); refusing to start or resume a turn"
                    )

                existing = self._sessions.get(key)
                recycling = existing is not None and owner._recycling.get(key) is existing
                if existing is not None and not recycling:
                    session = existing
                    if (
                        getattr(session.provider, "_private_memory", False) is True
                    ) != private_memory:
                        raise RuntimeError(
                            "Session runtime memory isolation does not match its trusted binding; "
                            "restart the session before continuing"
                        )
                    # The cwd match is NOT checked here. This runs under the registry
                    # lock but BEFORE the session semaphore is claimed, so a turn can
                    # be streaming on this provider right now -- evicting and shutting
                    # it down from here would tear a live reply down mid-stream, which
                    # is the very harm the deferred reset above exists to prevent.
                    # ``_reacquire_and_validate`` checks it instead: that runs with the
                    # semaphore HELD, so any turn that was streaming has finished and
                    # the eviction cannot land under one. See its own comment.
                    alive = session.provider.is_process_alive()
                    if not alive:
                        if (
                            self._deps.is_claude_provider(session.provider)
                            and session.provider.connection_mode == "per_session"
                        ):
                            self._deps.logger.info(
                                "Session %s CC process dead — will reconnect on next stream()",
                                key,
                            )
                            alive = True
                        else:
                            self._deps.logger.warning(
                                "Session %s has dead provider — removing stale entry",
                                key,
                            )
                            stale_provider = session.provider
                            stale_session = session
                            del self._sessions[key]
                            self.advance_ownership_generation(key)
                            # Same tick as the removal. Left unrecorded, the
                            # start crumb survives and the next boot calls this
                            # a crash rather than an eviction.
                            await record_session_ended(key, end_reason=END_REASON_EVICTED)
                    if alive:
                        session.last_used = time.monotonic()
                        if (
                            self._deps.is_claude_provider(session.provider)
                            and session.provider.session_id
                            and not owner._session_map.get(key)
                        ):
                            owner._session_map.set(
                                key,
                                session.provider.session_id,
                                provider=constants.provider_label_claude,
                                cwd=session.provider.cwd,
                            )
                        # The semaphore may be held for a full turn; claim it
                        # only after releasing the global registry lock.
                        claimed = session

                if claimed is None:
                    if not owner._provider_factory:
                        raise RuntimeError("No provider factory configured")
                    factory = owner._provider_factory
        finally:
            if stale_provider is not None:
                if stale_session is not None:
                    await asyncio.to_thread(self._deps.unlink_session_queue, stale_session)
                try:
                    await stale_provider.shutdown()
                except Exception:
                    self._deps.logger.warning(
                        "Failed to shut down stale provider for %s",
                        key,
                        exc_info=True,
                    )

        if claimed is not None:
            session = claimed
            if await owner._reacquire_and_validate(
                key,
                session,
                wait_if_busy=wait_if_busy,
                cwd=cwd,
            ):
                first_turn = session.first_turn
                if not speculative:
                    session.first_turn = self._deps.first_turn_nothing_armed
                return session.provider, first_turn.is_new, first_turn.resumed
            await owner._evict_stale_session(key, session)
            if not owner._provider_factory:
                raise RuntimeError("No provider factory configured")
            factory = owner._provider_factory

        # Model resolution reads agent JSON and therefore stays off the loop.
        if model is None:
            model = await asyncio.get_running_loop().run_in_executor(
                None,
                self._deps.session_model,
                owner._cfg,
                agent,
            )

        resume_sid: str | None = None
        is_stateless = (
            key in (constants.background_key, constants.heartbeat_key)
            or any(key.startswith(prefix) for prefix in constants.stateless_prefixes)
        ) and not owner._is_continuable_key(key)
        if not is_stateless:
            resume_sid = owner._session_map.get(key)
        if resume_sid and cwd == CWD_CLEARED:
            # The pool bypass below only stops a cleared claim taking a warm child; resuming
            # the stored SID would reinstate the conversation the clear was asked to drop.
            owner._session_map.clear_sid(key)
            resume_sid = None
        if speculative and resume_sid and not speculative_resume:
            raise SpeculativeResumeRefused(key)

        self._deps.logger.info(
            "Pool decision: key=%s resume_sid=%s model=%s agent=%s "
            "pool_size=%d pool_qsize=%d cwd=%s pool_cwd=%s",
            key,
            resume_sid,
            model,
            agent,
            owner._pool_size,
            owner._warm_pool.qsize(),
            cwd,
            owner._pool_cwd,
        )
        provider_switched = False
        # ``None`` states no preference, so the pool's shared binding is fine, but
        # CWD_CLEARED names the PER-SESSION default that no pooled child can be in.
        cwd_blocks_pool = cwd == CWD_CLEARED or bool(cwd and cwd != owner._pool_cwd)
        if not owner._pool_size:
            pool_decision = "disabled"
        elif private_memory:
            pool_decision = "bypass_private_memory"
        elif resume_sid:
            pool_decision = "bypass_resume"
        elif is_stateless:
            pool_decision = "bypass_stateless"
        elif self._is_member_key(key):
            # A pooled child was spawned with no session key, so it runs the
            # factory's DEFAULT backend and none of the member construction
            # route (per-session dispatch-tool mount, member backend). A warm
            # hit would silently hand a member DM a session that cannot mount
            # its tools; cold-starting through the factory is what makes the
            # member route real. String check — as cheap as the arms above.
            pool_decision = "bypass_member"
        elif cwd_blocks_pool:
            pool_decision = "bypass_cwd"
        elif extra_env:
            pool_decision = "bypass_env"
        elif await self._crew_pins_effort(agent, extra_factory_kwargs.get("crew_agent")):
            # A CREW's pinned effort is fixed at spawn time and the warm-pool
            # claim path never re-pushes it, so a warm hit would silently run
            # this crew at the wrong depth.  Cold-starting is what makes the
            # pin real.  (Caller-supplied reasoning_effort_override is handled
            # post-claim via provider.change_effort instead — see below.)
            #
            # Last in the chain on purpose: it is the only arm that needs to
            # read config, so every cheaper reason to skip the pool is settled
            # first and a bypassing session never pays for the lookup.
            pool_decision = "bypass_effort"
        else:
            pool_decision = ""

        pooled = None if pool_decision else await owner._drain_and_claim(agent)
        if not pool_decision:
            pool_decision = "hit" if pooled is not None else "miss_empty"
        owner._record_pool_decision(pool_decision, key)
        if pooled is not None:
            provider = pooled
            try:
                if self._deps.is_acp_provider(provider):
                    claim_kwarg = extra_factory_kwargs.get("crew_agent")

                    def resolve_claim_watchdog() -> tuple[str, object]:
                        # Resolve from a fresh config off-loop.  AcpClient.rekey
                        # resets prompt cost/context state while rebinding the
                        # handle and watchdog to the claiming crew.
                        config = self._deps.load_config()
                        crew = self._deps.resolve_crew_identity(
                            config,
                            agent,
                            None if claim_kwarg is None else str(claim_kwarg),
                        )
                        return crew, self._deps.load_watchdog_settings(crew)

                    claim_crew, claim_watchdog = await asyncio.to_thread(resolve_claim_watchdog)
                    cast(Any, provider).client.rekey(
                        key,
                        channel_id,
                        crew_agent=claim_crew,
                        watchdog=claim_watchdog,
                    )
                    if model:
                        pool_model = (
                            owner._resolve_agent_model(owner._pool_agent)
                            if owner._pool_agent
                            else None
                        )
                        if self._deps.is_claude_backend(provider):
                            switch_model = self._deps.to_provider_id(model, "claude_code")
                            comparable_pool = (
                                self._deps.to_provider_id(pool_model, "claude_code")
                                if pool_model
                                else pool_model
                            )
                        else:
                            switch_model = self._deps.to_acp_id(model)
                            comparable_pool = (
                                self._deps.to_acp_id(pool_model) if pool_model else pool_model
                            )
                        if pool_model and switch_model != comparable_pool:
                            try:
                                advertised = self._deps.advertised_model_ids(
                                    provider.available_models()
                                )
                            except Exception:  # pragma: no cover - defensive
                                advertised = []
                            _send_model = switch_model
                            if advertised and self._deps.model_is_unusable(
                                switch_model, advertised
                            ):
                                # A literal miss can be a stale `<namespace>::`
                                # qualifier on a model the backend fully serves:
                                # resolve to the advertised spelling and
                                # send THAT — the same fold the cold-start spawn
                                # and the display verdict use, so a warm claim
                                # runs exactly what a cold start of the same pin
                                # runs. A pin absent under either spelling still
                                # takes the withhold below.
                                _send_model = self._deps.resolve_pin_spelling(
                                    switch_model, advertised
                                )
                            if not _send_model:
                                self._deps.logger.warning(
                                    "Pool post-claim: model %s is not available to this "
                                    "account; leaving the claimed process on %s",
                                    switch_model,
                                    pool_model,
                                )
                            else:
                                await cast(Any, provider).client.set_model(_send_model)
                                self._deps.logger.info(
                                    "Pool post-claim: switched model to %s",
                                    _send_model,
                                )
                    _effort_override = extra_factory_kwargs.get("reasoning_effort_override")
                    if _effort_override:
                        _eff = str(_effort_override)
                        try:
                            if not await provider.change_effort(_eff):
                                _cur_model = (
                                    getattr(cast(Any, provider).client, "_model", None) or ""
                                )
                                self._deps.logger.warning(
                                    "reasoning effort '%s' will not be applied (session %s) — "
                                    "model '%s' does not support effort configuration",
                                    _eff,
                                    key or "?",
                                    _cur_model or "auto",
                                )
                            else:
                                _cur_model = (
                                    getattr(cast(Any, provider).client, "_model", None) or ""
                                )
                                self._deps.logger.info(
                                    "Pool post-claim: applied reasoning effort %s to model %s",
                                    _eff,
                                    _cur_model,
                                )
                        except Exception:
                            self._deps.logger.warning(
                                "Pool post-claim: failed to apply reasoning effort '%s' (session %s)",
                                _eff,
                                key or "?",
                                exc_info=True,
                            )
                self._deps.logger.info(
                    "Claimed warm-pool process for %s (agent=%s)",
                    key,
                    agent or owner._pool_agent,
                )
                owner._schedule_replenish()
            except (asyncio.CancelledError, Exception):
                owner._dispatch_hard_kill(provider)
                raise
        else:
            effective_cwd = cwd
            # `is None` and not falsy: an explicit `""` is a CLEARED project, and
            # restoring the persisted directory over it re-binds the old one forever.
            if effective_cwd is None and resume_sid:
                stored_cwd = owner._session_map.get_cwd(key)
                if stored_cwd and Path(stored_cwd).is_dir():
                    effective_cwd = stored_cwd
                    self._deps.logger.info("Resume CWD override for %s: %s", key, stored_cwd)
            provider = factory(
                key,
                agent=agent,
                channel_id=channel_id,
                model_override=model,
                cwd=effective_cwd,
                extra_env=extra_env,
                **extra_factory_kwargs,
            )
            if self._deps.is_acp_provider(provider):
                await cast(Any, provider).prepare_private_memory()
            if (getattr(provider, "_private_memory", False) is True) != private_memory:
                raise RuntimeError("Provider does not match the session's memory isolation")
            provider_switched = False
            if resume_sid:
                is_claude_now = self._deps.is_claude_provider(
                    provider
                ) or self._deps.is_claude_backend(provider)
                current_provider = (
                    constants.provider_label_claude
                    if is_claude_now
                    else self._deps.provider_label(provider)
                )
                if self._deps.detect_provider_switch(owner._session_map, key, current_provider):
                    resume_sid = None
                    provider_switched = True
                    owner._session_map.clear_sid(key)

            if resume_sid:
                if self._deps.is_acp_provider(provider):
                    cast(Any, provider).client.set_resume_session_id(resume_sid)
                    self._deps.logger.info(
                        "Attempting session/load for %s (sid=%s)", key, resume_sid
                    )
                elif self._deps.is_claude_provider(provider):
                    cast(Any, provider).set_resume_session_id(resume_sid)
                    self._deps.logger.info("CC resume for %s (sid=%s)", key, resume_sid)
            async with self._start_sem:
                try:
                    await provider.start()
                except (asyncio.CancelledError, Exception):
                    owner._dispatch_hard_kill(provider)
                    raise

        # start() has published the PID, but registry ownership is not visible
        # until the lock section below. Shield this narrow orphan-sweep window.
        starting_pid = getattr(getattr(provider, "client", None), "_pid", None)
        if not isinstance(starting_pid, int):
            process = getattr(provider, "_proc", None)
            starting_pid = (
                process.pid if process is not None and process.returncode is None else None
            )
        if not isinstance(starting_pid, int):
            starting_pid = None
        if starting_pid is not None:
            self._starting_pids.add(starting_pid)

        won_race_session: Any | None = None
        stale_generation = False
        duplicate_provider: LLMProvider | None = None
        # Resolved BEFORE the registry lock: the alias resolve loads config, and holding the
        # lock across that read is what wedges every other session (see the claim path).
        cold_arm = self._arm_if_any(key)
        cold_armed_alias = cold_arm.agent if cold_arm is not None else None
        cold_armed_target = (
            await asyncio.to_thread(self._deps.resolve_runtime_agent, cold_armed_alias, None)
            if cold_armed_alias
            else None
        )
        try:
            resumed = False
            if self._deps.is_acp_provider(provider):
                resumed = cast(Any, provider).client.resumed
            if speculative and speculative_resume and not resumed:
                raise SpeculativeResumeRefused(key)

            async with self._lock:
                # start() can span the complete close_all snapshot, so closing
                # must be checked a second time immediately before registration.
                if self._closing:
                    raise SessionClosingError(
                        "SessionManager began closing during provider startup; "
                        "refusing to register a session behind the shutdown snapshot"
                    )

                existing = self._sessions.get(key)
                recycling = existing is not None and owner._recycling.get(key) is existing
                if existing is not None and not recycling:
                    session = existing
                    if (
                        getattr(session.provider, "_private_memory", False) is True
                    ) != private_memory:
                        raise RuntimeError(
                            "Concurrent session runtime has incompatible memory isolation"
                        )
                    session.last_used = time.monotonic()
                    if approval_policy:
                        session.approval_policy = approval_policy
                    if agent:
                        session.agent = agent
                    won_race_session = session
                    duplicate_provider = provider
                else:
                    if not speculative:
                        first_turn = self._deps.first_turn_nothing_armed
                    elif resumed:
                        first_turn = self._deps.first_turn_resumed
                    else:
                        first_turn = self._deps.first_turn_fresh
                    session = self._deps.session_factory(
                        provider=provider,
                        first_turn=first_turn,
                        approval_policy=approval_policy,
                        agent=agent or "",
                    )
                    replay_needed = getattr(provider, "_history_replay_needed", False) is True
                    if provider_switched or replay_needed:
                        session.provider_switch_replay = True
                    if (
                        replay_needed
                        and self._deps.provider_label(provider) != constants.provider_label_default
                    ):
                        owner._session_map.clear_sid(key)
                    self._sessions[key] = session
                    self.advance_ownership_generation(key)
                    try:
                        await record_session_started(key)
                    except BaseException:
                        # See open_task_session: a cancellation here would leave a
                        # registered session whose provider the caller is about to
                        # kill, plus a crumb the next boot reads as a crash.
                        if self._sessions.get(key) is session:
                            del self._sessions[key]
                            self.advance_ownership_generation(key)
                        await discard_session_start(key)
                        raise
                    self._deps.logger.info(
                        "New session: %s agent=%s resumed=%s provider_switch=%s (total=%d)",
                        key,
                        agent or "kirocrew",
                        resumed,
                        provider_switched,
                        len(self._sessions),
                    )

                    provider_cwd = provider.cwd
                    claim_arm = self._arm_if_any(key)
                    armed_cwd = claim_arm.cwd if claim_arm is not None else None
                    armed_agent = claim_arm.agent if claim_arm is not None else None
                    # The arm holds an ALIAS while the retry re-points through the resolver,
                    # so both spellings must satisfy it. Resolved off-thread before the lock.
                    cold_target = (
                        cold_armed_target if cold_armed_alias == armed_agent else armed_agent
                    )
                    agent_contradicts_arm = armed_agent is not None and (
                        agent or "kirocrew"
                    ) not in (
                        armed_agent,
                        cold_target or armed_agent,
                    )
                    if (
                        started_generation < self._generation(key)
                        or (armed_cwd is not None and armed_cwd != resolved_cwd(provider_cwd, key))
                        or agent_contradicts_arm
                    ):
                        # Started before the change, bound where the arm contradicts, or
                        # running a switched-away agent -- see session.md for why all three.
                        stale_generation = True
                        session.retire_on_identity_change = True
                    # A STALE start's SID names the conversation the change discarded, and
                    # eviction happens later, so persisting it lets the retry resume it.
                    keep_sid = not stale_generation
                    if keep_sid and not is_stateless and self._deps.is_acp_provider(provider):
                        sid = cast(Any, provider).client._session_id
                        provider_label = self._deps.provider_label(provider)
                        if sid:
                            owner._session_map.set(
                                key,
                                sid,
                                provider=provider_label,
                                cwd=provider_cwd,
                            )
                    elif keep_sid and not is_stateless and self._deps.is_claude_provider(provider):
                        sid = provider.session_id
                        if sid:
                            owner._session_map.set(
                                key,
                                sid,
                                provider=constants.provider_label_claude,
                                cwd=provider_cwd,
                            )

                    # Cleanup owns its task slot; allocation only asks the
                    # facade to ensure it at the original registration point.
                    owner._ensure_cleanup_task()
                    # Fresh semaphore acquisition is synchronous and cannot
                    # wait, so doing it under _lock does not invert lock order.
                    await session.semaphore.acquire()
                    self._deps.inc_session_created()
                    result = (provider, True, resumed)
        except BaseException:
            owner._dispatch_hard_kill(provider)
            raise
        finally:
            if starting_pid is not None:
                self._starting_pids.discard(starting_pid)

        if stale_generation:
            # This frame already holds the new session's semaphore, so the won-race
            # branch below cannot serve it: that path re-acquires and would self-block.
            try:
                await owner._evict_stale_session(key, session)
            finally:
                # The eviction AWAITS the registry lock, so a cancellation there would
                # leave this session registered holding a permit nothing ever releases.
                session.semaphore.release()
            maximum = constants.won_race_max_retries
            if _won_race_retries >= maximum:
                raise RuntimeError(
                    f"get_or_create({key!r}) exceeded {maximum} won-race retries — "
                    "a cold start kept starting before the project change it must honour"
                )
            # Re-pointed at the arm like `cwd` below: the retry must be able to SATISFY what
            # refused it, or the budget runs out and the slot wedges.
            retry_arm_rec = self._arm_if_any(key)
            retry_agent = retry_arm_rec.agent if retry_arm_rec is not None else None
            if retry_agent is None:
                retry_target = agent
            elif retry_agent == cold_armed_alias:
                retry_target = cold_armed_target or retry_agent
            else:
                # A SECOND switch re-armed the key AFTER the resolve above, so this alias has
                # no resolved target yet and the raw name would reach the provider as a mode.
                retry_target = (
                    await asyncio.to_thread(self._deps.resolve_runtime_agent, retry_agent, None)
                    or retry_agent
                )
            retry_arm = retry_arm_rec.cwd if retry_arm_rec is not None else None
            if retry_arm_rec is not None and retry_arm_rec.requires_sid_clear:
                # A cwd left by an earlier project change is not CWD_CLEARED, so the resume
                # guard below would not fire; the discard's own clear_sid may not have landed.
                owner._session_map.clear_sid(key)
                retry_arm_rec.requires_sid_clear = False
            return await owner.get_or_create(
                key,
                agent=retry_target,
                channel_id=channel_id,
                approval_policy=approval_policy,
                model=model,
                cwd=(retry_arm if retry_arm is not None else cwd),
                extra_env=extra_env,
                speculative=speculative,
                speculative_resume=speculative_resume,
                wait_if_busy=wait_if_busy,
                _won_race_retries=_won_race_retries + 1,
                **extra_factory_kwargs,
            )

        if won_race_session is not None:
            if duplicate_provider is not None:
                try:
                    await duplicate_provider.shutdown()
                except Exception:
                    self._deps.logger.warning(
                        "Failed to shut down duplicate provider for %s",
                        key,
                        exc_info=True,
                    )
            if await owner._reacquire_and_validate(
                key,
                won_race_session,
                wait_if_busy=wait_if_busy,
                cwd=cwd,
            ):
                first_turn = won_race_session.first_turn
                if not speculative:
                    won_race_session.first_turn = self._deps.first_turn_nothing_armed
                return (
                    won_race_session.provider,
                    first_turn.is_new,
                    first_turn.resumed,
                )
            maximum = constants.won_race_max_retries
            if _won_race_retries >= maximum:
                raise RuntimeError(
                    f"get_or_create({key!r}) exceeded {maximum} won-race retries — "
                    "session kept going stale between acquire and re-validate"
                )
            # The armed directory outranks the cwd this frame was called with: that one
            # was read before the change, so reusing it would re-lose the same race.
            live_rec = self._arm_if_any(key)
            live_arm = live_rec.cwd if live_rec is not None else None
            retry_cwd = live_arm if live_arm is not None else cwd
            return await owner.get_or_create(
                key,
                agent=agent,
                channel_id=channel_id,
                approval_policy=approval_policy,
                model=model,
                cwd=retry_cwd,
                extra_env=extra_env,
                speculative=speculative,
                speculative_resume=speculative_resume,
                wait_if_busy=wait_if_busy,
                _won_race_retries=_won_race_retries + 1,
                **extra_factory_kwargs,
            )

        return result

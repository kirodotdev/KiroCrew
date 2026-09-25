"""Share one ``AcpRuntime`` process across several top-level chat sessions.

Subagents already multiplex sessions onto their parent's process. This module
extends the same demux to top-level dashboard chat slots, which otherwise spawn
one ``kiro-cli`` process each.

Three rules govern it, and each exists because breaking it is silently wrong
rather than loudly wrong:

``eligible_for_chat_sharing``
    Only a dashboard chat slot running in persistent memory mode may share. A
    cron, hook, task-runner or crew-member session keeps its own process, and so
    does an incognito or temporary session -- see the function's own reasoning.

:class:`ChatRuntimeKey`
    Two sessions may land on one process only when every PROCESS-LEVEL spawn
    input matches. A per-session input (the ACP ``cwd``, the session key, the
    crew identity) is deliberately absent from the key, because ``create_session``
    carries it per session. An input whose scope is ambiguous is IN the key: a
    key that is too narrow costs memory, while one that is too wide runs a
    session under another session's process configuration.

:class:`ChatRuntimePool`
    A runtime lives while at least one session holds it. The last release kills
    it; every earlier release only drops a reference. The cap bounds how many
    sessions one process serves, which is the blast radius of that process
    dying. Placement is the first compatible runtime with room for the session,
    otherwise a fresh process.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from kiro_crew.acp_backends import ACP_BACKENDS_SESSION_SHARING
from kiro_crew.messaging.link import telemetry_channel_of

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.agent_sdk.tool_search import ToolSearchSettings

logger = logging.getLogger(__name__)

#: Memory modes that may share a process. A shared process OUTLIVES any single
#: session on it, because the other sessions hold the reference -- so an
#: incognito or temporary session's teardown cannot take the process (and the
#: scratch files it wrote) with it, which is exactly what that mode promises.
#: ``create_session`` also latches ``recording_allowed`` off for the WHOLE
#: runtime when it starts a non-persistent session, so one such session joining
#: would stop recording for every other session on the process.
_SHAREABLE_MEMORY_MODES = frozenset({"persistent"})


def eligible_for_chat_sharing(
    *,
    session_key: str | None,
    memory_mode: str,
    member_context: bool,
    sharing_enabled: bool,
    backend: str,
) -> bool:
    """Whether this session may join (or found) a shared chat runtime.

    Scoped to dashboard chat slots. ``telemetry_channel_of`` is the repository's
    single classification of a session key's origin, and it answers
    ``"dashboard"`` only for a chat slot -- a cron, hook, task-runner, subagent
    or channel-bound key classifies as something else, so each keeps its own
    process without this function naming any of them.

    A chat slot is necessary but NOT sufficient. ``backend`` must also be a host
    whose single process serves several sessions, asked as membership in
    ``ACP_BACKENDS_SESSION_SHARING`` -- the same capability the subagent sharing
    path already requires. A host outside it is served by ``AcpRuntime`` yet maps
    ``destroy()`` onto an irreversible server-side session delete, where keeping
    the transcript protects nothing: the pooled teardown destroys this session's
    handle to leave a process it may not kill, so on such a host every ordinary
    chat close would discard its own resume record. Membership is asked rather
    than a specific host being excluded, so a host added later is out until
    someone puts it in deliberately.

    This cannot be settled by the compatibility key: two sessions on the SAME
    unsupported backend have equal keys, so they would pool with each other,
    which is the broken case rather than a safe one.

    ``member_context`` is refused because a crew member's process captures that
    member's native launch documents at spawn, so its process is already
    member-specific.
    """
    if not sharing_enabled:
        return False
    if backend not in ACP_BACKENDS_SESSION_SHARING:
        return False
    if member_context:
        return False
    if memory_mode not in _SHAREABLE_MEMORY_MODES:
        return False
    return telemetry_channel_of(session_key) == "dashboard"


def _freeze_env(extra_env: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    """A hashable, order-independent form of the child's extra environment."""
    if not extra_env:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in extra_env.items()))


def _freeze_path(value: str | Path | None) -> str:
    """A comparable spelling of a path-shaped spawn input (``""`` when unset).

    Both spellings of one directory have to land on the same key, and a bare
    ``str()`` does not deliver that: on Windows a ``Path`` stringifies with
    backslashes while a caller's plain string keeps the separators it was
    written with, so the same work directory would key two processes and the
    pool would spawn instead of sharing. Coercing through ``Path`` first gives
    one spelling per platform, and it also folds a trailing separator, which is
    the same directory by every other measure.

    Deliberately NOT ``resolve()``: this runs on the placement path for every
    session start, and resolving touches the filesystem and can raise. The key
    compares what the caller asked to spawn with, which is what the pre-spawn
    gates judge too.
    """
    if not value:
        return ""
    return str(Path(value))


def _freeze_tool_search(settings: "ToolSearchSettings | None") -> tuple[object, ...]:
    """The Tool Search choice as sent at ``initialize``, or ``()`` when unset.

    Sent once per process in ``clientCapabilities``, so a session cannot carry
    its own -- a session joining a process configured differently would run with
    a setting it did not ask for.
    """
    if settings is None:
        return ()
    return (
        bool(settings.enabled),
        int(settings.min_pct),
        int(settings.min_tokens),
    )


@dataclass(frozen=True)
class ChatRuntimeKey:
    """Every process-level spawn input of one ``AcpRuntime``.

    One line per field on why it cannot vary between two sessions on a process:

    ``work_dir``
        The child's own cwd, and the directory four pre-spawn gates judge:
        derived-spec freshness, fork governance, the sealed-target refusal and
        the voice-workspace assertion. ``kiro-cli`` also resolves ``--agent``
        against ``<work_dir>/.kiro/agents`` before the global directory, so the
        spec a session runs follows the process, not the session.
    ``agent``
        Carried on argv as ``--agent``, and rewritten in place by the native
        skill projection.
    ``model``
        NOT about which model a session runs: a host that pins one at process
        start takes it on argv, but the chat path applies a model per session
        after ``session/new``, so two sessions on one process each run their own.
        It is in the key because the effort overlay's row is NAMED by the model.
        That file is one per work directory and ``kiro-cli`` reads it once at
        startup, so two slots agreeing on the effort LEVEL still disagree on
        effort DELIVERY when their models differ: the process loaded a table
        holding only the founder's row, and the joiner runs at its own model's
        default. Keying effort alone does not close that.
    ``sandbox_mode``
        Chooses the wrap, the credential mask and whether the host's internal
        sandbox is delegated to.
    ``extra_env``
        The child's environment, which one process has exactly one of.
    ``acp_backend``
        Selects the harness, and through it the binary and the handshake.
    ``tool_search``
        Sent once in the ``initialize`` handshake.
    ``member_context``
        Decides whether native launch documents are captured at spawn.

        An eligibility MIRROR, not a discriminator: ``eligible_for_chat_sharing``
        refuses a member session outright, so the only value that ever reaches
        this key is ``False``. That single admitted value is the reason the field
        stays -- it is the second line of a two-line defence over a property that
        is unsafe to share, and eligibility cannot be folded into the key (see
        that function's own note on equal keys). Removing it as a constant
        removes a guard.
    ``memory_mode``
        Latches the runtime's recording permission at ``create_session``.

        The same mirror: ``_SHAREABLE_MEMORY_MODES`` admits one mode, so this
        field also carries a single value on every pooled key, and for the same
        reason it stays.
    ``shared_scratch``
        The session tree's work directory, mounted into the process.
    ``mcp_gateway_overlay`` / ``mcp_gateway_socket``
        Held on the runtime and read when composing each session's MCP array.
        Scope is ambiguous, so both are in the key.
    ``reasoning_effort``
        The level the spawn writes into the work directory's ``cli.json``
        overlay, which ``kiro-cli`` reads once at startup. The overlay is one
        file per work directory keyed by model, so two sessions asking for
        different levels would have the last writer decide for the whole
        process and a joining session would silently run at the founder's
        level.
    """

    work_dir: str
    agent: str
    model: str
    sandbox_mode: str
    extra_env: tuple[tuple[str, str], ...]
    acp_backend: str
    tool_search: tuple[object, ...]
    member_context: bool
    memory_mode: str
    shared_scratch: str
    mcp_gateway_overlay: str
    mcp_gateway_socket: str
    reasoning_effort: str

    @classmethod
    def build(
        cls,
        *,
        work_dir: str | Path | None,
        agent: str,
        sandbox_mode: str,
        extra_env: dict[str, str] | None,
        acp_backend: str,
        tool_search: "ToolSearchSettings | None",
        member_context: bool,
        memory_mode: str,
        shared_scratch: Path | None,
        mcp_gateway_overlay: str | Path | None,
        mcp_gateway_socket: str | Path | None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> "ChatRuntimeKey":
        """Build the key from the values a caller is about to spawn with.

        Keyword-only and exhaustive on purpose: a field added to the runtime's
        constructor and forgotten here would widen the key silently, which is
        the failure this shape makes impossible to do by accident.

        Exhaustive is not the same as self-enforcing, so the shape is locked from
        outside as well: a structural test walks the process-level spawn inputs
        AND the workspace ``cli.json`` writers against these fields, and fails on
        any name that is neither keyed nor listed with a reason. Two channels
        because an input can reach the process as a parameter or as a file.
        """
        return cls(
            work_dir=_freeze_path(work_dir),
            agent=agent or "",
            model=model or "",
            sandbox_mode=sandbox_mode or "",
            extra_env=_freeze_env(extra_env),
            acp_backend=acp_backend or "",
            tool_search=_freeze_tool_search(tool_search),
            member_context=bool(member_context),
            memory_mode=memory_mode or "",
            shared_scratch=_freeze_path(shared_scratch),
            mcp_gateway_overlay=_freeze_path(mcp_gateway_overlay),
            mcp_gateway_socket=_freeze_path(mcp_gateway_socket),
            reasoning_effort=reasoning_effort or "",
        )


@dataclass
class _Entry:
    """One live runtime and the LEASES held on it.

    Keyed by lease rather than by session key, because a session key is not a
    count. The allocator legitimately starts the same session twice at once (it
    carries a race budget for exactly that), and both starts must be releasable
    on their own: if the two shared one reference, the loser's teardown would
    drop it and the winner's live process would be killed mid-turn.
    """

    runtime: "AcpRuntime"
    key: ChatRuntimeKey
    leases: dict[str, str] = field(default_factory=dict)
    #: False once this runtime's credential has been retired. The process keeps
    #: serving the sessions already on it -- they are torn down by the identity
    #: sweep on their own terms -- but no NEW session may land here, because the
    #: credential it authenticated with is the one that was retired. Nothing in
    #: the compatibility key can express this: the key describes what a session
    #: ASKS to spawn with, and every session asks for the same thing before and
    #: after a credential change.
    claimable: bool = True

    def has_room(self, cap: int) -> bool:
        return len(self.leases) < cap

    def session_keys(self) -> list[str]:
        return sorted(set(self.leases.values()))


@dataclass
class Acquisition:
    """What :meth:`ChatRuntimePool.acquire` handed back.

    ``lease`` is this acquisition's own handle and the ONLY thing that releases
    it. Two acquisitions -- even for one session key -- get two leases, and the
    runtime dies when the last of them is gone.

    ``joined`` is False for the acquisition that FOUNDED the runtime and True for
    every one that landed on an existing one. Callers use it for the two
    decisions that differ: whether the provider owns the process, and whether
    per-session start work that rewrites process-level state may run.
    """

    runtime: "AcpRuntime"
    joined: bool
    leases_on_runtime: int
    lease: str


class ChatRuntimePool:
    """Registry of shared chat runtimes, keyed by process-level compatibility.

    One lock serializes the whole registry rather than one lock per key. The
    critical section is a dict lookup plus, for a miss, one spawn, and holding it
    across that spawn is a deliberate trade of concurrency for simplicity: the
    bookkeeping this lock protects -- entry list, lease index, last-runtime map --
    stays provably consistent because nothing else can observe it mid-spawn.

    The cost is real and is not zero, so do not restate it as free. The
    gateway-wide cold-start admission gate admits TWO concurrent spawns
    (``runtime._COLD_START_MAX_CONCURRENT``), so a second cold start is entitled
    to proceed and this lock stops it; a join that would land on a DIFFERENT,
    already-warm runtime waits behind the same multi-second cold start, for which
    it has no need at all. Moving the spawn out of the critical section and
    re-checking the registry on re-entry is the known remedy, and it is a
    behaviour change to concurrency and lease accounting rather than a comment
    fix, so it is tracked as follow-up work instead of being done here.
    """

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._by_lease: dict[str, _Entry] = {}
        self._last_for_session: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    # ── writes ──

    async def acquire(
        self,
        key: ChatRuntimeKey,
        session_key: str,
        spawn: Callable[[], Awaitable["AcpRuntime"]],
        *,
        cap: int,
    ) -> Acquisition:
        """Take a LEASE on a runtime, spawning one only when needed.

        Placement: the runtime this session key last landed on, when it is still
        alive, compatible and has room -- which keeps a restart on the process
        its history is on -- then the first compatible runtime with room,
        otherwise ``spawn()``. Compatible means an equal :class:`ChatRuntimeKey`,
        and room means fewer than *cap* leases are held on it.

        Every call mints its OWN lease, including a second call for a session key
        that already holds one. That is the point rather than an oversight: the
        allocator legitimately starts one session twice at once, and each start
        has to be releasable without ending the other.

        A dead runtime is never handed out and never counted: it is dropped on
        the way past, so the caller that finds none alive spawns, and the
        sessions that were on it rejoin through this same path on their own next
        turn.
        """
        async with self._lock:
            self._drop_dead_locked()
            entry = self._pick_locked(key, cap=cap, session_key=session_key)
            joined = entry is not None
            if entry is None:
                runtime = await spawn()
                entry = _Entry(runtime=runtime, key=key)
                self._entries.append(entry)
            lease = uuid.uuid4().hex
            entry.leases[lease] = session_key
            self._by_lease[lease] = entry
            self._last_for_session[session_key] = entry
            logger.info(
                "chat_runtime_sharing outcome=%s pid=%s leases=%d cap=%d runtimes=%d",
                "joined" if joined else "spawned",
                getattr(entry.runtime, "pid", None),
                len(entry.leases),
                cap,
                len(self._entries),
            )
            return Acquisition(
                runtime=entry.runtime,
                joined=joined,
                leases_on_runtime=len(entry.leases),
                lease=lease,
            )

    async def release(self, lease: str) -> "AcpRuntime | None":
        """Drop ONE lease; return the runtime to kill only if it was the last.

        The runtime is returned rather than killed here so the caller performs
        the teardown it already performs, with its own logging and error
        handling. Returns None while any other lease is outstanding, which is
        what stops one session's close, reset or model switch -- or the teardown
        of a start that lost a same-key race -- from killing a process another
        acquisition is still using.
        """
        async with self._lock:
            entry = self._by_lease.pop(lease, None)
            if entry is None:
                return None
            entry.leases.pop(lease, None)
            if entry.leases:
                logger.info(
                    "chat_runtime_sharing outcome=released pid=%s remaining_leases=%d",
                    getattr(entry.runtime, "pid", None),
                    len(entry.leases),
                )
                return None
            self._forget_entry_locked(entry)
            logger.info(
                "chat_runtime_sharing outcome=last_release pid=%s",
                getattr(entry.runtime, "pid", None),
            )
            return entry.runtime

    async def forget(self, runtime: "AcpRuntime") -> list[str]:
        """Drop every lease on *runtime* and return the session keys that held it.

        Called when a runtime is known dead. Each returned session recovers on
        its own next turn through the ordinary start path, which finds no live
        runtime and acquires one -- the first to arrive spawns, the rest
        join it.
        """
        async with self._lock:
            for entry in list(self._entries):
                if entry.runtime is runtime:
                    orphaned = entry.session_keys()
                    self._forget_entry_locked(entry)
                    if orphaned:
                        logger.warning(
                            "chat_runtime_sharing outcome=runtime_lost pid=%s sessions=%d: "
                            "each recovers on its next turn",
                            getattr(runtime, "pid", None),
                            len(orphaned),
                        )
                    return orphaned
            return []

    def retire_pid(self, pid: int) -> int:
        """Stop NEW sessions landing on *pid*; returns how many entries it marked.

        The identity sweep calls this through ``kiro_crew.agent_sdk`` when a
        credential change retires the sessions a process is serving. A busy
        session is only MARKED by that sweep rather than removed, so its lease
        survives and keeps the entry alive and otherwise claimable -- and the
        compatibility key cannot tell the two eras apart, because a session asks
        to spawn with the same inputs before and after the change. Left
        claimable, the next start on that key would land on the process holding
        the retired credential and execute there.

        The sessions already on the process are NOT disturbed: the sweep owns
        their retirement, and the last lease released still kills the runtime
        the ordinary way.

        Flips a flag on entries that already exist, so it needs no lock: there
        is no await between this write and any read of it, and the registry's
        own shape is untouched -- which also means it cannot deadlock against a
        release in flight.
        """
        marked = 0
        for entry in self._entries:
            if getattr(entry.runtime, "pid", None) != pid or not entry.claimable:
                continue
            entry.claimable = False
            marked += 1
        if marked:
            logger.info("chat_runtime_sharing outcome=retired_pid pid=%s entries=%d", pid, marked)
        return marked

    def pid_has_outstanding_leases(self, pid: int) -> bool:
        """Whether a LIVE pooled runtime on *pid* still has leases outstanding.

        The reset path asks this through ``kiro_crew.agent_sdk`` to decide
        whether a pid that outlived one session's shutdown is a survivor or a
        co-tenant's live process. It closes a window the session table cannot
        see: a joining session takes its lease inside ``provider.start``, but is
        registered as a live session only once start RETURNS, so for the whole
        cold start a co-tenant's reset would otherwise find no survivor and kill
        the shared process under it.

        Liveness is probed HERE rather than inferred from the registry. Dead
        entries are dropped by ``_drop_dead_locked``, which runs inside
        ``acquire`` -- so between a runtime's death and the next acquisition its
        entry is still registered with its leases intact. Counting one would
        report a dead process as a co-tenant and suppress both its reap and the
        sweep of the children that escaped it.

        Deliberately not locked. It only reads, there is no ``await`` in the
        scan, and the caller runs on the same event loop the pool is mutated
        from -- so it observes a consistent snapshot without being able to
        deadlock against a release in flight.
        """
        for entry in self._entries:
            if not entry.leases:
                continue
            if getattr(entry.runtime, "pid", None) != pid:
                continue
            if not self._alive(entry):
                continue
            return True
        return False

    # ── internals ──

    def _pick_locked(self, key: ChatRuntimeKey, *, cap: int, session_key: str) -> _Entry | None:
        sticky = self._last_for_session.get(session_key)
        if (
            sticky is not None
            and any(e is sticky for e in self._entries)
            and sticky.key == key
            and sticky.claimable
            and sticky.has_room(cap)
            and self._alive(sticky)
        ):
            return sticky
        for entry in self._entries:
            if entry.key == key and entry.claimable and entry.has_room(cap) and self._alive(entry):
                return entry
        return None

    @staticmethod
    def _alive(entry: _Entry) -> bool:
        probe = getattr(entry.runtime, "is_alive", None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:
            return False

    def _drop_dead_locked(self) -> None:
        for entry in list(self._entries):
            if not self._alive(entry):
                orphaned = entry.session_keys()
                self._forget_entry_locked(entry)
                if orphaned:
                    logger.warning(
                        "chat_runtime_sharing outcome=dead_runtime_dropped pid=%s sessions=%d",
                        getattr(entry.runtime, "pid", None),
                        len(orphaned),
                    )

    def _forget_entry_locked(self, entry: _Entry) -> None:
        for lease in list(entry.leases):
            if self._by_lease.get(lease) is entry:
                del self._by_lease[lease]
        for session_key in entry.session_keys():
            if self._last_for_session.get(session_key) is entry:
                del self._last_for_session[session_key]
        entry.leases.clear()
        self._entries = [e for e in self._entries if e is not entry]


#: The gateway's one chat runtime pool. A module-level singleton because the
#: sharing decision has to be global: two chat sessions can only land on one
#: process if both consult the same registry.
CHAT_RUNTIME_POOL = ChatRuntimePool()

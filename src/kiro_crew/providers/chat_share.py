"""Chat runtime sharing on the AcpRuntime start path: where a chat session lands.

Behind the ``chat_runtime_sharing`` feature preview (off by default). With it off,
or for any start :func:`~kiro_crew.acp.chat_runtime_sharing.chat_sharing_ineligible_reason`
refuses, :func:`resolve_chat_share` answers ``None`` and the start spawns its own
process exactly as before.

An eligible start builds a :class:`~kiro_crew.acp.chat_runtime_sharing.ChatRuntimeKey`
from its process-level spawn inputs and asks the ownership registry for a lease
under it: the registry either hands back a live compatible runtime with room
(JOIN) or runs this start's spawn as its miss path (FOUND). The placement decision
and the spawn are one act, which is why the lease is taken here, before the
provider exists, and handed to ``AcpSessionProvider`` as ``runtime_lease``.

Four inputs a running process cannot shed are read before the acquisition and
RE-READ after it: the agent spec generation, the account era, the governed
sandbox tier and the SSH_AUTH_SOCK-forwarding consent. The acquisition is not
instantaneous (a same-key arrival waits out a founding spawn), so a revocation, a
sign-in to another account, a raised floor or a revoked consent landing in that
window would otherwise place this session on a process started under the older
value. A mismatch releases and retries on the fresh key; a value still moving
after :data:`_PLACEMENT_ATTEMPTS` rounds founds this session its own process. A
JOINER also runs the fork-governance admission the founder ran inside ``spawn()``.

Every release here runs as one task this frame only waits on, through any
cancellation, so a lease is never stranded: a stranded lease keeps its entry
alive, the kill gate refuses every kill of that pid, and the process leaks for
the gateway's life.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.acp.chat_runtime_sharing import (
    ChatRuntimeKey,
    agent_spawns_native_subagents,
    agent_spec_generation,
    chat_runtime_cap,
    chat_runtime_sharing_enabled,
    chat_sharing_ineligible_reason,
    kas_chat_sharing_ready,
)
from kiro_crew.acp.runtime import AcpRuntimeError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.runtime_ownership import CHAT_RUNTIME_CAP, RUNTIME_OWNERSHIP, Acquisition

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.acp import AcpProvider

logger = logging.getLogger(__name__)

#: How many placements a start makes while a keyed input keeps moving between the
#: read and the acquisition. Small on purpose: an input still moving after this
#: many rounds is being rewritten continuously, and the last round founds this
#: session its own process instead.
_PLACEMENT_ATTEMPTS = 3

#: Prefixes of a spec generation that is a token unique to one read
#: (``agent_spec_generation`` for an unreadable or unresolved spec, and the
#: give-up below). A key carrying one can never be joined, and a re-read never
#: equals it, so it is not re-compared.
_UNIQUE_GENERATION_PREFIXES = ("unobservable-", "unresolved-", "unsettled-")


async def _settle_release(lease: str) -> AcpRuntime | None:
    """Release *lease* and return the runtime the release hands back, through any cancellation.

    The release runs as one task this frame waits on in a shield loop, so a
    second cancellation cannot strand the lease the first one was unwinding. The
    caller re-raises its own exception afterwards.
    """
    task = asyncio.ensure_future(RUNTIME_OWNERSHIP.release(lease))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return cast("AcpRuntime | None", task.result())


async def _kill_stranded(runtime: AcpRuntime | None, reason: str) -> None:
    """Kill a runtime a release handed back (this start held its only lease)."""
    if runtime is None:
        return
    try:
        await runtime.kill(expected=True, reason=reason)
    except Exception:
        logger.debug("chat runtime sharing: stranded-runtime kill failed", exc_info=True)


class ChatSharePlacement:
    """One eligible chat start's placement: its key, its lease, and whether it joined."""

    def __init__(
        self,
        provider: AcpProvider,
        *,
        work_dir: str | Path,
        agent: str,
        sandbox_mode: str,
        extra_env: dict[str, str],
        mcp_gateway_overlay: Any,
        mcp_gateway_socket: Any,
        configured_model: str,
        launch_agent: str,
        session_key: str,
        cap: int,
        spec_generation: str,
        sandbox_tier: str,
        forward_ssh: bool,
        agent_env: dict[str, str] | None = None,
        kas_telemetry: bool = True,
    ) -> None:
        self._provider = provider
        # ``agent.env`` as this start read it: a child takes it at spawn and never
        # re-reads it, so a saved change must found a new process rather than join
        # one that carries the old values. A founding spawn launches with this same
        # snapshot (``spawn_agent_env``), so a process always carries what its key says.
        self._agent_env = dict(agent_env or {})
        # The telemetry opt-out as this start read it (KAS only; True elsewhere). KAS
        # takes it once at ``initialize``, so a founding spawn sends this same value
        # (``spawn_kas_telemetry``) and a process always carries what its key says.
        self._kas_telemetry = kas_telemetry
        self._work_dir = work_dir
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self._extra_env = extra_env
        self._overlay = mcp_gateway_overlay
        self._socket = mcp_gateway_socket
        self._model = configured_model
        self._launch_agent = launch_agent
        self.session_key = session_key
        self.cap = cap
        self._spec_generation = spec_generation
        self._sandbox_tier = sandbox_tier
        self._forward_ssh = forward_ssh
        # The RAW era this start observed. An empty read (no reader wired, or a
        # failed read) never becomes an empty KEY field: two failed reads would
        # compare equal and join across accounts. It becomes a token unique to
        # this start, which no other key can equal, so the session founds its
        # own process. The confirm compares raw against raw.
        self._raw_era = str(getattr(provider, "pre_spawn_identity", "") or "")
        self._era = self._raw_era or f"unverified-{uuid.uuid4().hex}"
        self._identity_reader = getattr(provider, "spawn_identity_reader", None)
        # False once placement gave up and made the key unique: nothing can join
        # it, so the spawn reads the forwarding consent fresh instead of the
        # frozen one.
        self.joinable = True
        self.lease: str | None = None
        self.joined = False
        self.leases_on_runtime = 0
        self.key = self._build_key()

    def _build_key(self) -> ChatRuntimeKey:
        """This session's key from the state as it stands NOW (the scratch tree can move)."""
        p = self._provider
        return ChatRuntimeKey.build(
            work_dir=self._work_dir,
            agent=self._agent,
            sandbox_mode=self._sandbox_tier,
            extra_env=self._extra_env,
            acp_backend=p._client.backend,
            tool_search=p._tool_search_settings(),
            memory_mode=p.memory_mode,
            shared_scratch=p._shared_scratch,
            mcp_gateway_overlay=self._overlay,
            mcp_gateway_socket=self._socket,
            model=self._model,
            reasoning_effort=p._resolve_effort(),
            spawn_identity=self._era,
            spec_generation=self._spec_generation,
            forward_ssh_auth_sock=self._forward_ssh,
            launch_agent=self._launch_agent,
            harness_pin=getattr(p, "_harness_pin", "") or "",
            agent_env=self._agent_env,
            kas_telemetry=self._kas_telemetry,
        )

    def spawn_agent_env(self) -> dict[str, str]:
        """The ``agent.env`` snapshot a founding spawn launches with: the one keyed on."""
        return dict(self._agent_env)

    def spawn_kas_telemetry(self) -> bool:
        """The telemetry opt-out a founding KAS spawn sends: the one keyed on."""
        return self._kas_telemetry

    def spawn_forward_ssh(self) -> bool | None:
        """The forwarding request a founding spawn uses: frozen on a joinable key, else fresh."""
        return self._forward_ssh if self.joinable else None

    async def _read_confirmation(self) -> tuple[str, str, str, bool, bool]:
        """Re-read the five unsheddable inputs after the acquisition."""
        from kiro_crew.kiro_prerequisite import pre_spawn_identity
        from kiro_crew.sandbox import _forward_ssh_auth_sock, effective_sandbox_mode

        generation = await asyncio.to_thread(agent_spec_generation, self._work_dir, self._agent)
        if self._identity_reader is not None:
            era = await pre_spawn_identity(self._identity_reader)
        else:
            era = self._raw_era
        forward = await asyncio.to_thread(_forward_ssh_auth_sock, self._sandbox_mode, ())
        tier = await asyncio.to_thread(effective_sandbox_mode, self._sandbox_mode)
        telemetry = await asyncio.to_thread(
            _kas_telemetry_for,
            self._provider._client.backend,
            self._work_dir,
            self._agent_env,
            self._extra_env,
        )
        return generation, era, tier, forward, telemetry

    async def _admit_joiner(self, placed: Acquisition) -> None:
        """Run the fork-governance admission a founder ran inside ``spawn()``.

        A joiner lands on a live runtime without spawning, so it never passed
        that gate. A fork whose refresh failed leaves the spec bytes (and so the
        key) unchanged, which is exactly when a joiner matches. Refused or
        cancelled, the joiner's lease is released before the error propagates.
        """
        from kiro_crew.agent import ForkGovernanceUnresolved, require_fork_governance

        try:
            await asyncio.to_thread(require_fork_governance, self._agent, self._work_dir)
        except ForkGovernanceUnresolved as exc:
            await _kill_stranded(
                await _settle_release(placed.lease), "chat runtime sharing: join refused"
            )
            raise AcpRuntimeError(str(exc)) from exc
        except BaseException:
            await _kill_stranded(
                await _settle_release(placed.lease), "chat runtime sharing: join cancelled"
            )
            raise

    async def place(self, spawn: Callable[[], Awaitable[AcpRuntime]]) -> AcpRuntime:
        """Take a lease on a compatible runtime (or found one), then confirm the placement."""
        for _ in range(_PLACEMENT_ATTEMPTS):
            placed = await RUNTIME_OWNERSHIP.acquire(
                self.key, self.session_key, spawn, cap=self.cap
            )
            try:
                generation, era, tier, forward, telemetry = await self._read_confirmation()
            except BaseException:
                await _kill_stranded(
                    await _settle_release(placed.lease),
                    "chat runtime sharing: placement cancelled before confirm",
                )
                raise
            # An empty re-read is not evidence the account held: it fails closed
            # to a retry, where the empty era becomes a unique token. A key whose
            # pre-read era was already empty carries such a token and could
            # never have joined, so it is not re-compared.
            era_holds = (not self._raw_era) or (bool(era) and era == self._raw_era)
            generation_holds = (
                self._spec_generation.startswith(_UNIQUE_GENERATION_PREFIXES)
                or generation == self._spec_generation
            )
            if (
                generation_holds
                and era_holds
                and tier == self._sandbox_tier
                and forward == self._forward_ssh
                and telemetry == self._kas_telemetry
            ):
                if placed.joined:
                    await self._admit_joiner(placed)
                return self._accept(placed)
            logger.info(
                "chat runtime sharing: a keyed input moved during placement "
                "(spec=%s era=%s tier=%s forward=%s telemetry=%s); retrying on the fresh key",
                not generation_holds,
                not era_holds,
                tier != self._sandbox_tier,
                forward != self._forward_ssh,
                telemetry != self._kas_telemetry,
            )
            await _kill_stranded(
                await _settle_release(placed.lease),
                "chat runtime sharing: placement moved before confirm",
            )
            self._spec_generation = generation
            self._raw_era = era
            self._era = era or f"unverified-{uuid.uuid4().hex}"
            self._sandbox_tier = tier
            self._forward_ssh = forward
            self._kas_telemetry = telemetry
            self.key = self._build_key()
        # Still moving: a key nothing can match founds this session its own
        # process, the answer ``agent_spec_generation`` gives an unobservable file.
        self._spec_generation = f"unsettled-{uuid.uuid4().hex}"
        self.joinable = False
        self.key = self._build_key()
        logger.warning(
            "chat runtime sharing: keyed inputs kept moving across %d placements; "
            "this session takes its own process",
            _PLACEMENT_ATTEMPTS,
        )
        placed = await RUNTIME_OWNERSHIP.acquire(self.key, self.session_key, spawn, cap=self.cap)
        return self._accept(placed)

    def _accept(self, placed: Acquisition) -> AcpRuntime:
        self.lease = placed.lease
        self.joined = placed.joined
        self.leases_on_runtime = placed.leases_on_runtime
        runtime = cast("AcpRuntime", placed.runtime)
        # One line per placement at WARNING, beside the refusal line: an operator
        # counting processes needs both. The key is not logged (paths, env).
        logger.warning(
            "chat-runtime-sharing %s runtime=%s tenants=%d session=%s",
            "joined" if placed.joined else "spawned",
            runtime.pid,
            placed.leases_on_runtime,
            self.session_key,
        )
        return runtime

    async def replace_dead(self, spawn: Callable[[], Awaitable[AcpRuntime]]) -> AcpRuntime:
        """Place again after the runtime died during a resume.

        The lease on the corpse is released first, so the table forgets the dead
        entry, and the key is rebuilt: the provider's scratch tree just moved onto
        the dead process's. Through the same confirmed placement as the cold start.
        """
        if self.lease is not None:
            lease, self.lease = self.lease, None
            await _settle_release(lease)
        self.key = self._build_key()
        return await self.place(spawn)

    async def abandon(self, runtime: AcpRuntime, session_id: str | None) -> None:
        """Undo a start that failed after placement, without ending co-tenants.

        The session (when one was created) is evicted from the process, keeping
        its record, and the lease released; the runtime is killed only when this
        start held its last lease. Runs to completion through any cancellation: a
        cancellation landing in the eviction still returns the lease (the release
        is in a ``finally``) and is re-raised after it, so the process never leaks.
        """
        try:
            if session_id:
                try:
                    if getattr(runtime, "acp_backend", None) == ACP_BACKEND_KAS:
                        await runtime.evict_session(session_id)
                    else:
                        await runtime.terminate_session(session_id)
                except Exception:
                    logger.debug(
                        "chat runtime sharing: failed-start eviction failed", exc_info=True
                    )
        finally:
            lease, self.lease = self.lease, None
            if lease is not None:
                await _kill_stranded(
                    await _settle_release(lease), "failed session setup cleanup (last holder)"
                )


def _kas_telemetry_for(
    backend: str, work_dir: str | Path, agent_env: dict[str, str], extra_env: dict[str, str]
) -> bool:
    """Whether a KAS process founded for this start would send telemetry on; True elsewhere.

    Judged on the child's environment in launch's order (the gateway's, agent.env,
    then extra_env), as ``_handshake_client_capabilities`` judges it: KAS takes the
    opt-out once, at ``initialize``, so a joiner cannot shed a founder's value.
    """
    if backend == ACP_BACKEND_KAS:
        from kiro_crew.acp.kas_cli_settings import kiro_cli_telemetry_enabled

        return kiro_cli_telemetry_enabled(work_dir, {**os.environ, **agent_env, **extra_env})
    # Every other backend sends no opt-out at initialize, so all its keys agree.
    return True


async def resolve_chat_share(
    provider: AcpProvider,
    *,
    work_dir: str | Path,
    agent: str,
    sandbox_mode: str,
    extra_env: dict[str, str],
    mcp_gateway_overlay: Any,
    mcp_gateway_socket: Any,
    configured_model: str,
    launch_agent: str,
    remote_workspace: bool,
) -> ChatSharePlacement | None:
    """This start's placement, or ``None`` to spawn its own process as before.

    ``None`` whenever the preview is off (one config read, nothing else), and for
    every start the eligibility rules refuse; a refusal is logged at WARNING only
    while the preview is on, naming the rule. Only an eligible start makes the
    placement reads (KAS engine readiness, spec generation, sandbox tier,
    forwarding consent), in one hop off the event loop.
    """
    try:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = live.snapshot()
        if cfg is None:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
        enabled = chat_runtime_sharing_enabled(cfg)
        configured_cap = int(
            getattr(cfg.agent, "chat_runtime_sharing_max_sessions", CHAT_RUNTIME_CAP)
        )
    except Exception:
        # An unreadable config leaves sharing OFF: absence must not enable a
        # feature nobody turned on.
        logger.debug("chat runtime sharing: config unreadable", exc_info=True)
        return None
    if not enabled:
        return None
    backend = provider._client.backend
    session_key = provider._owning_session_key()
    kas_ready = False
    if backend == ACP_BACKEND_KAS:
        kas_ready = await asyncio.to_thread(kas_chat_sharing_ready)
    refusal = chat_sharing_ineligible_reason(
        session_key=session_key,
        memory_mode=provider.memory_mode,
        sharing_enabled=enabled,
        backend=backend,
        member_context=provider.member_context,
        remote_workspace=remote_workspace,
        kas_engine_ready=kas_ready,
    )
    if not refusal and await asyncio.to_thread(agent_spawns_native_subagents, work_dir, agent):
        refusal = "native_subagents"
    if refusal:
        logger.warning("chat-runtime-sharing ineligible reason=%s session=%s", refusal, session_key)
        return None
    from kiro_crew.acp.agent_env import agent_env_overlay
    from kiro_crew.sandbox import _forward_ssh_auth_sock, effective_sandbox_mode

    def _read_placement_inputs() -> tuple[str, str, bool, dict[str, str], bool]:
        configured_env = agent_env_overlay()
        telemetry = _kas_telemetry_for(backend, work_dir, configured_env, extra_env)
        return (
            agent_spec_generation(work_dir, agent),
            effective_sandbox_mode(sandbox_mode),
            _forward_ssh_auth_sock(sandbox_mode, ()),
            configured_env,
            telemetry,
        )

    generation, tier, forward, configured_env, telemetry = await asyncio.to_thread(
        _read_placement_inputs
    )
    return ChatSharePlacement(
        provider,
        work_dir=work_dir,
        agent=agent,
        sandbox_mode=sandbox_mode,
        extra_env=extra_env,
        mcp_gateway_overlay=mcp_gateway_overlay,
        mcp_gateway_socket=mcp_gateway_socket,
        configured_model=configured_model,
        launch_agent=launch_agent,
        session_key=session_key,
        cap=chat_runtime_cap(sharing_enabled=True, configured=configured_cap),
        spec_generation=generation,
        sandbox_tier=tier,
        forward_ssh=forward,
        agent_env=configured_env,
        kas_telemetry=telemetry,
    )


__all__ = ["ChatSharePlacement", "resolve_chat_share"]

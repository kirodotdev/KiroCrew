"""Companion and run runtimes: the shared ACP processes a parent session's work runs on.

A companion runtime hosts a parent session's subagents in one process
(``get_subagent_runtime`` / ``release_subagent_runtime``), and a run runtime hosts the
per-step task sessions of one run (``_get_or_bootstrap_run_runtime``; the task session
itself is opened by ``SessionAllocationService.open_task_session``, which stays on the
class because it imports from ``kiro_crew.acp``). This module also decides whether a
subagent may share its parent's runtime (``is_session_sharing_eligible`` and the
project-AWS-profile split), mirrors the parent's spawn posture onto a companion
(``_collect_parent_runtime_kwargs``, ``parent_work_scratch_dir``), and lists the
runtimes' processes (``runtime_pids``, ``_append_companion_runtime_rows``).

Each ``self``-first function here is a ``SessionAllocationService`` member: its body is
the method's, and the class binds it by name, so the instance and its registry state
stay on the class. ``kiro_crew.session_allocation`` re-exports the two module-level
helpers and is the path callers import them from. This module never imports the facade
at run time.

Instance fields written: ``_starting_pids``, ``_subagent_runtime_locks``, ``_subagent_runtimes``.

Instance fields read: ``_deps``, ``_owner``, ``_sessions``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.kas_floor_wait import await_kas_floor_verdict
from kiro_crew.aws_project_profiles import configured_project_profiles, resolve_project_profile
from kiro_crew.kiro_prerequisite import pre_spawn_identity, spawn_pid, stamp_spawn_identity
from kiro_crew.start_priority import START_QUEUE_COMPANION, StartPriority, notify_start_queue
from kiro_crew.validation import bounded_session_id

if TYPE_CHECKING:
    from kiro_crew.session_allocation import SessionAllocationService, _AllocationOwner


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
    # The MCP Tool Search choice rides the runtime constructor on a wire-settings
    # host, so a companion runtime built without it would run with the setting
    # left to the host's default rather than the explicit value the parent sent.
    # Read off the LLMProvider capability (safe default None), never probed.
    tool_search = provider.tool_search_settings
    if tool_search is not None:
        kwargs["tool_search"] = tool_search
    # The parent's session tree keeps ONE work directory across every process
    # it spans: a companion runtime is handed the parent's ``$KIROCREW_SCRATCH``
    # as a second private window into the masked scratch root, so a brief the
    # parent staged there is readable by the subagents the runtime hosts (see
    # ``agent_scratch``); at spawn it joins the tree's owner marker beside the parent.
    shared_scratch = parent_work_scratch_dir(owner, parent_session_key)
    if shared_scratch is not None:
        kwargs["shared_scratch"] = shared_scratch
    return kwargs


def parent_work_scratch_dir(owner: _AllocationOwner, parent_session_key: str) -> Path | None:
    """The work directory of *parent_session_key*'s session tree, or None.

    Read off the parent's live provider through the ``LLMProvider``
    capability (``work_scratch_dir``, harness-parity H14 -- declared on the
    ABC with a ``None`` default, never probed for a private name); None when
    the parent has no live provider or its process carries no scratch. The
    caller passes it as ``shared_scratch`` to the spawn it makes on the
    parent's behalf -- a companion runtime here, a dedicated subagent process
    in ``subagent_manager/run_turn.py`` -- and the spawn re-validates it at mount
    time (``agent_scratch.shared_scratch_window``).
    """
    provider = owner.get_provider(parent_session_key)
    if provider is None:
        return None
    path = provider.work_scratch_dir
    return path if isinstance(path, Path) else None


async def get_subagent_runtime(
    self: SessionAllocationService,
    parent_session_key: str,
    agent: str | None = None,
    *,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    on_gate_queued: Callable[..., None] | None = None,
    on_gate_acquired: Callable[..., None] | None = None,
) -> Any:
    """Get or spawn the canonical shared companion runtime for a parent.

    ``start_priority`` orders the runtime's own cold start at the spawn
    admission; a task run a person opened passes FOREGROUND through
    ``_get_or_bootstrap_run_runtime``.

    ``on_gate_queued(queue)`` / ``on_gate_acquired(queue_wait_ms, queue)`` are a
    waiting start's clock callbacks. They bracket the two WAITS only: the
    per-parent lock (``queue`` = :data:`START_QUEUE_COMPANION`), behind a sibling
    that is spawning the runtime, and this caller's own spawn admission, which
    the spawn brackets itself. The caller's own spawn work is not bracketed, so
    a spawn that hangs stays on the caller's running clock.
    """
    runtime_type, runtime_dead = self._deps.runtime_types()
    max_retries = 1
    attempt = 0
    selected_agent = agent
    spawn_kwargs: dict[str, Any] = {"start_priority": start_priority}
    if on_gate_queued is not None or on_gate_acquired is not None:
        spawn_kwargs["on_gate_queued"] = on_gate_queued
        spawn_kwargs["on_gate_acquired"] = on_gate_acquired
    while True:
        lock = self._subagent_runtime_locks.setdefault(parent_session_key, asyncio.Lock())
        notify_start_queue(self._deps.logger, on_gate_queued, START_QUEUE_COMPANION)
        waited_from = time.monotonic()
        try:
            await lock.acquire()
        finally:
            notify_start_queue(
                self._deps.logger,
                on_gate_acquired,
                (time.monotonic() - waited_from) * 1000.0,
                START_QUEUE_COMPANION,
            )
        try:
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
                    await existing.kill(
                        reason="reaping a dead shared subagent runtime before respawn"
                    )
                except Exception:
                    self._deps.logger.debug(
                        "get_subagent_runtime: dead runtime kill failed for %s",
                        parent_session_key,
                        exc_info=True,
                    )
            selected_agent = (
                selected_agent or self._owner._get_session_agent(parent_session_key) or "kirocrew"
            )
            kwargs = self._owner._parent_runtime_kwargs(parent_session_key)
            runtime = runtime_type(agent=selected_agent, **kwargs)
            # Bracket the spawn with identity reads so this runtime carries
            # a spawn stamp: every subagent session demuxed onto it
            # inherits its credential, and the registry pass in
            # ``flag_identity_stamp_mismatches`` can only compare a stamp
            # that was recorded. The stamp lands on the runtime object
            # itself (the gate reads ``runtime.spawn_identity`` directly).
            pre_spawn = await pre_spawn_identity(
                getattr(self._owner, "spawn_identity_reader", None)
            )
            try:
                await runtime.spawn(**spawn_kwargs)
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
            # Best-effort spawn-account record; see flag_identity_stamp_mismatches.
            # A cancellation landing in this read would leak the spawned
            # runtime before it reaches the registry below, so tear it
            # down on the way out. The read is also a suspension point
            # before the registry (the orphan sweep's companion-PID union)
            # can see this runtime, so shield its PID for the span.
            starting_pid = spawn_pid(runtime)
            if starting_pid is not None:
                self._starting_pids.add(starting_pid)
            try:
                try:
                    await stamp_spawn_identity(
                        getattr(self._owner, "spawn_identity_reader", None),
                        runtime,
                        pre_spawn=pre_spawn,
                    )
                except BaseException:
                    with contextlib.suppress(Exception):
                        await runtime.kill(
                            expected=True,
                            reason="spawn-stamp interrupted before registration",
                        )
                    raise
                self._subagent_runtimes[parent_session_key] = runtime
            finally:
                if starting_pid is not None:
                    self._starting_pids.discard(starting_pid)
            return runtime
        finally:
            lock.release()


async def release_subagent_runtime(
    self: SessionAllocationService, parent_session_key: str, *, expected: Any = None
) -> bool:
    """Serialize release with spawn and kill the detached runtime off-map.

    ``expected`` pins the release to ONE runtime object: the pop happens
    only while that object is still the registered one. A caller that
    decided to release from a snapshot (the identity sweep) waits for the
    per-parent lock behind a respawn in flight; that respawn installs a
    replacement under the same key before letting go, and a pop by key
    alone would then kill the replacement the caller never looked at.
    Returns whether a runtime was popped (and so killed).
    """
    lock = self._subagent_runtime_locks.get(parent_session_key)
    if lock is not None:
        async with lock:
            if (
                expected is not None
                and self._subagent_runtimes.get(parent_session_key) is not expected
            ):
                return False
            runtime = self._subagent_runtimes.pop(parent_session_key, None)
            # A waiter on this removed lock re-checks canonical identity in
            # get_subagent_runtime and retries under the live lock.
            self._subagent_runtime_locks.pop(parent_session_key, None)
    else:
        if expected is not None and self._subagent_runtimes.get(parent_session_key) is not expected:
            return False
        runtime = self._subagent_runtimes.pop(parent_session_key, None)
    if runtime is None:
        return False
    try:
        await runtime.kill(expected=True, reason="subagent runtime released")
    except Exception:
        self._deps.logger.warning(
            "Failed to kill subagent runtime for %s",
            parent_session_key,
            exc_info=True,
        )
    return True


async def _get_or_bootstrap_run_runtime(
    self: SessionAllocationService,
    parent_session_key: str,
    *,
    agent: str | None = None,
    cwd: str | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> Any:
    """Adopt a configured bootstrap provider's runtime for a task run."""
    owner = self._owner
    if not owner._provider_factory:
        # Outside the per-key lock: get_subagent_runtime takes that lock and
        # asyncio.Lock is not reentrant.
        return await owner.get_subagent_runtime(
            parent_session_key, agent=agent, start_priority=start_priority
        )

    # A defaulted KAS waits (bounded) for the kiro-cli floor's verdict, so a task
    # run started at boot gets the backend the verdict decided.
    from kiro_crew.members import is_member_session_key

    await await_kas_floor_verdict(member=is_member_session_key(parent_session_key))
    if parent_session_key not in self._subagent_runtime_locks:
        self._subagent_runtime_locks[parent_session_key] = asyncio.Lock()
    lock = self._subagent_runtime_locks[parent_session_key]
    async with lock:
        existing = self._subagent_runtimes.get(parent_session_key)
        if existing is not None and existing.is_alive():
            return existing
        if existing is not None:
            # A runtime marked dead on a stdin stall still has a live child
            # that may read the frames buffered in its pipe; it is killed
            # BEFORE its replacement starts, so a retried step never runs
            # beside it (the reap ``get_subagent_runtime`` does too).
            try:
                await existing.kill(reason="reaping a dead task-run runtime before respawn")
            except Exception:
                self._deps.logger.debug(
                    "run runtime: dead runtime kill failed for %s",
                    parent_session_key,
                    exc_info=True,
                )
            reader_probe = getattr(existing, "stdin_reader_may_live", None)
            if (
                getattr(existing, "stdin_stall_death", False) is True
                and callable(reader_probe)
                and await asyncio.to_thread(reader_probe) is True
            ):
                # A process that can still read the stalled pipe survived
                # the kill (a signal that did not land, or a launcher child
                # that outlived its parent): a replacement now would run
                # beside it. The step's bounded retry ladder runs this
                # bootstrap, and so this kill, again on its next attempt.
                raise RuntimeError(
                    "a stalled task-run runtime could not be confirmed dead; "
                    "not starting a replacement beside it"
                )
        provider = owner._provider_factory(parent_session_key, agent=agent, cwd=cwd)
        provider.start_priority = start_priority
        pre_spawn = await pre_spawn_identity(getattr(owner, "spawn_identity_reader", None))
        await provider.start()
        # The stamp read below suspends before this provider's runtime is
        # visible to the orphan sweep's companion-PID union -- shield the
        # freshly-published PID for the whole start-to-registration span.
        starting_pid = spawn_pid(provider)
        if starting_pid is not None:
            self._starting_pids.add(starting_pid)
        try:
            # Record which account the store held as this runtime spawned:
            # every session later demuxed onto it inherits this credential, so
            # the stamp lives on the runtime's wrapping provider and is read
            # back through the ``_runtime`` fallback in
            # ``flag_identity_stamp_mismatches``. Best-effort; an unstamped
            # runtime keeps the pre-stamping protections. A cancellation
            # landing in this read would leak the started provider before it
            # reaches the registry below, so kill it on the way out.
            try:
                await stamp_spawn_identity(
                    getattr(owner, "spawn_identity_reader", None), provider, pre_spawn=pre_spawn
                )
            except BaseException:
                owner._dispatch_hard_kill(provider)
                raise
            session_provider = getattr(provider, "_client", None)
            runtime = getattr(session_provider, "_runtime", None)
            if session_provider is not None and runtime is not None:
                # Sessions demuxed onto this runtime wrap it as ``_runtime``
                # on their own providers, so the stamp must live on the
                # runtime object itself for the ``_runtime`` fallback in
                # ``flag_identity_stamp_mismatches`` to find it.
                spawn_stamp = getattr(provider, "spawn_identity", "")
                if spawn_stamp:
                    try:
                        runtime.spawn_identity = spawn_stamp
                    except Exception:
                        self._deps.logger.debug(
                            "run runtime refused the spawn identity stamp", exc_info=True
                        )
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
            # This provider derived the PARENT's work directory from the
            # parent's key only to bootstrap a runtime; the directory is
            # the parent session's, whatever the factory flagged.
            provider.disown_work_dir()
            try:
                await provider.shutdown()
            except Exception:
                self._deps.logger.debug(
                    "run runtime bootstrap provider shutdown failed", exc_info=True
                )
        finally:
            if starting_pid is not None:
                self._starting_pids.discard(starting_pid)
    return await owner.get_subagent_runtime(
        parent_session_key, agent=agent, start_priority=start_priority
    )


def _get_session_agent(self: SessionAllocationService, session_key: str) -> str:
    session = self._sessions.get(session_key)
    if session is None:
        return ""
    return getattr(session, "agent", "") or ""


def _parent_runtime_kwargs(
    self: SessionAllocationService, parent_session_key: str
) -> dict[str, Any]:
    return _collect_parent_runtime_kwargs(self._owner, parent_session_key)


def is_session_sharing_eligible(self: SessionAllocationService, parent_session_key: str) -> bool:
    # Exact-key lookup is current behavior; do not fold this seam here.
    session = self._sessions.get(parent_session_key)
    if session is None:
        return False
    if session.loaded_capabilities is not None:
        return False
    return getattr(session.provider, "is_session_sharing_eligible", False)


def _parent_aws_profile(self: SessionAllocationService, parent_session_key: str) -> str | None:
    """The profile ``aws.project_profiles`` set on the parent's process, if any."""
    session = self._sessions.get(parent_session_key)
    recorded = getattr(session, "aws_project_profile", None)
    return recorded if isinstance(recorded, str) else None


def aws_profile_split_possible(self: SessionAllocationService, parent_session_key: str) -> bool:
    """Whether a subagent of the parent could need another AWS profile than it.

    A subagent on the shared arm runs inside the parent's process (or a
    companion runtime that mirrors the parent's environment), so it carries
    whatever profile ``aws.project_profiles`` set on the parent at its cold
    start; the dedicated arm sets the profile bound to the subagent's own
    ``cwd``. Those can differ only while the map holds a binding or the
    parent carries one. Reads no path, so a synchronous caller may ask.
    """
    if configured_project_profiles(self._owner._cfg):
        return True
    return self._parent_aws_profile(parent_session_key) is not None


def project_aws_profile(self: SessionAllocationService, cwd: str) -> str | None:
    """The profile ``aws.project_profiles`` binds *cwd* to, or ``None``.

    Resolves every mapped directory with ``realpath``, so a directory on a
    mount that stops answering blocks the calling thread: an async caller
    runs this off the event loop.
    """
    project_profiles = configured_project_profiles(self._owner._cfg)
    if not project_profiles or not cwd:
        return None
    return resolve_project_profile(project_profiles, cwd)


def aws_profile_differs_from_parent(
    self: SessionAllocationService, parent_session_key: str, child_profile: str | None
) -> bool:
    """Whether a subagent bound to *child_profile* differs from the parent's profile.

    *child_profile* is the subagent's own binding, already resolved
    (:meth:`project_aws_profile`); a bound parent with an unbound child
    differs, and so does the reverse. Reads no path.
    """
    return child_profile != self._parent_aws_profile(parent_session_key)


def _runtime_pid(runtime: Any) -> int | None:
    pid = getattr(runtime, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


def runtime_pids(self: SessionAllocationService) -> list[dict[str, object]]:
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
                # The ACP session id, which is also the id of this session's
                # crew log unit; the Sessions table's lineage reader joins on
                # it. Backend-authored, so bounded here where it is retained
                # (the bound every other store of this id applies); an
                # oversize or empty value is carried as None, not truncated.
                "sid": bounded_session_id(getattr(session.provider, "session_id", None)),
                "pid": self._runtime_pid(runtime),
                "owns_runtime": bool(getattr(client, "_owns_runtime", True)),
                "created_at": session.created_at,
                "prompts": session.prompt_count,
            }
        )
    self._owner._append_companion_runtime_rows(rows)
    return rows


def _append_companion_runtime_rows(
    self: SessionAllocationService, rows: list[dict[str, object]]
) -> None:
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
                now_wall - (now_monotonic - spawned) if isinstance(spawned, (int, float)) else None
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

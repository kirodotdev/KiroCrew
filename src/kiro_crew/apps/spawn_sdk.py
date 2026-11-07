"""Spawn SDK — app-scoped background agent spawning.

An app doing unattended work (a poller, a watcher, a planner) needs to start an
agent turn with no user present. The host already owns that machinery
(``SubagentManager``: concurrency accounting, a reaper for wedged agents, SEL
audit), so this is a permissioned adapter onto it, not a second spawner.

It arrives the way ``cron`` / ``events`` / ``storage`` do — declared as
``permissions.spawn``, injected by the gateway, ``None`` otherwise — so "which
apps may start an agent" reads off the manifest rather than off an import graph.
The core never imports an app to hand it this.

``run`` RAISES on refusal rather than returning a falsy id: the host can decline
(approval policy, caps), and a caller that reads a decline as success waits on a
spawn that never happened instead of counting a failure and backing off.

``continue_`` hands a follow-up task to a finished run's conversation, so a
pipeline that returns work to the same agent keeps that agent's context. It runs
the same checks as ``run`` against the agent the conversation was started with,
and an expired or busy conversation raises with a ``code`` the app can switch on
to fall back to a fresh ``run``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Awaitable, Callable

from kiro_crew.agent_discovery import is_internal_agent_spec, list_agents
from kiro_crew.sel import sel
from kiro_crew.subagent_manager.admission.types import TASK_STORE_UNAVAILABLE_CODE
from kiro_crew.subagent_wait_reasons import stopped_before_start

logger = logging.getLogger(__name__)


def _audit_spawn_denied(app: str, agent: str, reason: str, tool_name: str = "spawn_run") -> None:
    """Emit a SEL denial record for an SDK-side spawn refusal.

    The SDK rejects an empty-agent, unverifiable, or cross-app spawn BEFORE it
    reaches ``SubagentManager`` (whose own governance gate emits the denial
    record for spawns that get that far). Without this, an authorization refusal
    at the SDK boundary would leave no security-event trail. Best-effort: an
    audit failure must never turn a denial into a crash.
    """
    try:
        sel().log_tool_invocation(
            session_key="",
            source="app_spawn_sdk",
            tool_name=tool_name,
            outcome="denied",
            error=reason,
            metadata={"app": app, "agent": agent or "default"},
        )
    except Exception:  # noqa: BLE001 — auditing must never break the refusal
        logger.warning("Failed to SEL-audit denied spawn for app %r", app)


#: What the gateway injects: ``(task, agent, silent, model, app) -> awaitable[spawn id]``.
#: ``app`` is the calling app's name — threaded so the spawn's governance check
#: can resolve that app's OWN profile (blast-radius containment), not just the
#: policy ceiling. An empty id means the host declined. :meth:`SpawnSDK.run`
#: passes ``keep=True`` as a keyword only when the app asked for it.
SpawnImpl = Callable[..., Awaitable[str]]

#: Optional continuation the gateway injects alongside the impl:
#: ``(conversation, task, model, silent, app) -> awaitable[run id]``.
ContinueImpl = Callable[[str, str, str, bool, str], Awaitable[str]]

#: :attr:`SpawnError.code` when the conversation has no resumable session left
#: (expired, released, or pruned). Start a fresh run instead.
CONVERSATION_GONE = "conversation_gone"
#: :attr:`SpawnError.code` when a run is still in flight on the conversation (or
#: another continuation of it is starting). Wait for it, or start a fresh run.
CONVERSATION_BUSY = "conversation_busy"


def _app_spawn_model(app: str, agent: str) -> str:
    """The user's ``apps.<name>.model`` for a spawn that picked no model, or ``""``.

    ``""`` -- no setting, an agent that pins its own model, or a config that could
    not be read -- leaves the spawn on the model it runs on without the setting.
    Blocking (config load and agent JSON reads): call off the loop. Never raises.
    """
    try:
        # Lazy: this SDK module imports no config at load time.
        from kiro_crew.config.loader import KiroCrewConfig, resolve_app_spawn_model

        return resolve_app_spawn_model(KiroCrewConfig.load(), app, agent)
    except Exception:  # noqa: BLE001 — an unreadable setting must not refuse the spawn
        logger.warning("Could not read the model setting for app %r", app, exc_info=True)
        return ""


class SpawnError(RuntimeError):
    """The host declined or could not start the requested agent.

    ``declined`` separates two kinds of failure. ``True`` is a HOST-PRESSURE
    decline — the admission gate had no capacity to START this spawn and did not
    accept it either: a task-store refusal (a memory wait it had no durable row
    to park, or a store write that failed), which the gate marks with
    ``error_code`` == ``task_store_unavailable`` (``build_spawn_impl`` recognises
    that code), or it returned nothing at all (an empty id). A caller may treat
    this as a back-off signal. ``False`` is a programming or impl fault (empty
    task, empty agent, a normalised impl exception, or any other refusal) that
    no amount of waiting fixes.

    An accepted-but-deferred spawn is NOT a decline and raises nothing: the gate
    returns a queued record with a real id, so ``build_spawn_impl`` hands that id
    back like any other accepted spawn. The caller must treat a returned id as
    in flight (queued or running) and not re-submit it.

    A caller that backed off on the whole exception type would pause on faults
    too, so the flag is the signal to gate on — not ``isinstance``.

    ``code`` names a refusal a caller acts on differently. ``continue_`` sets it
    to :data:`CONVERSATION_GONE` or :data:`CONVERSATION_BUSY`, both of which a
    caller answers with a fresh ``run``; every other refusal leaves it ``""``.
    """

    def __init__(self, *args: object, declined: bool = False, code: str = "") -> None:
        super().__init__(*args)
        self.declined = declined
        self.code = code


#: Optional probe the gateway injects alongside the impl:
#: ``(spawn_id) -> bool`` — True once that agent has finished (any outcome).
DoneProbe = Callable[[str], bool]


class SpawnSDK:
    """App-scoped view of the host's background-agent spawning."""

    def __init__(
        self,
        app_name: str,
        impl: SpawnImpl,
        done_probe: DoneProbe | None = None,
        continue_impl: ContinueImpl | None = None,
    ) -> None:
        self._app_name = app_name
        self._impl = impl
        self._done_probe = done_probe
        self._continue_impl = continue_impl

    def is_done(self, spawn_id: str) -> bool:
        """True once the spawned agent has finished (success OR failure).

        An unattended caller that serializes its spawns (a poller, a planner)
        needs a completion signal to release its lock early instead of always
        sitting out the full timeout. Unknown ids read as done so a caller can
        never wedge on an id the host has already forgotten. Returns False when
        the host injected no probe — the caller's timeout then stands alone.
        """
        if self._done_probe is None:
            return False
        try:
            return bool(self._done_probe(spawn_id))
        except Exception:  # noqa: BLE001 — a probe failure must not break the caller
            return False

    async def run(
        self,
        task: str,
        agent: str = "",
        *,
        silent: bool = False,
        model: str = "",
        keep: bool = False,
    ) -> str:
        """Start a background agent for *task*; return its spawn id.

        ``agent`` is REQUIRED and must name the app's own restricted background
        agent. An unattended spawn is bounded by that agent's tool surface, not by
        an approval prompt (there is no user to ask, so ``approval_mode="auto"``),
        and ``capabilities.spawn.scopes.agents`` is enforced against the NAMED
        agent. An empty name would fall through to the host's DEFAULT agent — full
        tool surface, auto-approved — AND skip the agent-scope evaluation, so a
        profile that restricts spawning to specific agents would be silently
        bypassed. The parameter keeps its ``""`` default only so the failure is a
        loud raise here rather than a TypeError. ``model`` overrides the agent's
        default model for this run — unattended work is where users most want a
        cheaper model, since nobody is present to weigh cost against quality. With
        no ``model``, the user's own ``apps.<name>.model`` setting applies unless
        the agent pins a model. ``keep=True`` is the host's ``keep`` flag: the run
        gets its own process and its conversation is retained for hours rather than
        about one, for an app that expects to :meth:`continue_` it later.
        """
        if not task.strip():
            raise SpawnError("spawn task is empty")
        if not agent.strip():
            reason = (
                f"spawn for app {self._app_name!r} requires a named agent: an empty "
                "agent would run the host default with full, auto-approved tools and "
                "skip the spawn agent-scope policy"
            )
            _audit_spawn_denied(self._app_name, agent, reason)
            raise SpawnError(reason)
        try:
            # ``keep`` rides as a keyword only when asked for, so an impl that
            # predates it still serves every spawn that does not ask.
            extra = {"keep": True} if keep else {}
            spawn_id = await self._impl(task, agent, silent, model, self._app_name, **extra)
        except SpawnError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise to one failure type
            raise SpawnError(f"spawn failed for app {self._app_name!r}: {exc}") from exc
        if not spawn_id:
            raise SpawnError(
                f"host declined the spawn for app {self._app_name!r} "
                f"(agent={agent or 'default'!r})",
                declined=True,
            )
        logger.info(
            "App %s spawned background agent %s (id=%s, silent=%s)",
            self._app_name,
            agent or "default",
            spawn_id,
            silent,
        )
        return spawn_id

    async def continue_(
        self, spawn_id: str, task: str, *, model: str = "", silent: bool = False
    ) -> str:
        """Send *task* as a follow-up turn to finished run *spawn_id*; return the new run id.

        The follow-up runs on the same conversation, so the agent keeps what it
        read and reasoned in the earlier run. *spawn_id* is the id :meth:`run`
        returned, and it stays the conversation's id for every later follow-up.
        The new id is for :meth:`is_done`.

        The checks are those of :meth:`run`: the conversation must be one this app
        started with one of its own ``<app>--*`` agents, the app's governance
        profile and ``capabilities.spawn`` scopes apply to that agent, and the run
        is auto-approved. The follow-up keeps the run's model unless *model*
        names another.

        Raises :class:`SpawnError`. Its ``code`` is :data:`CONVERSATION_GONE` when
        the conversation has nothing left to resume and :data:`CONVERSATION_BUSY`
        when a run is still in flight on it; either way a fresh :meth:`run` with a
        summary of the earlier result is the fallback.
        """
        if not task.strip():
            raise SpawnError("continue task is empty")
        if not spawn_id.strip():
            raise SpawnError("continue needs the spawn id of a finished run")
        if self._continue_impl is None:
            raise SpawnError(f"this host cannot continue a run for app {self._app_name!r}")
        try:
            run_id = await self._continue_impl(spawn_id, task, model, silent, self._app_name)
        except SpawnError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalise to one failure type
            raise SpawnError(f"continue failed for app {self._app_name!r}: {exc}") from exc
        if not run_id:
            raise SpawnError(
                f"host declined the continuation for app {self._app_name!r}", declined=True
            )
        logger.info(
            "App %s continued run %s (id=%s, silent=%s)",
            self._app_name,
            spawn_id,
            run_id,
            silent,
        )
        return run_id


async def _own_agents(app: str, agent: str, tool_name: str) -> set[str]:
    """The names of *app*'s own agents; raises (audited) when they cannot be read.

    Off the loop: ``list_agents()`` scans the agents dir and JSON-parses each file.
    On a cold or large directory that read would block the gateway loop —
    stalling chat and the heartbeat — so it runs in a thread.

    Materialized app agents are named ``<app>--<agent>.json``, so the filename
    prefix is the ownership proof. Without it an app could name the GLOBAL host
    agent ("kirocrew") or another app's agent — which the manager would then run
    with ``approval_mode="auto"``, a full-privilege escalation from the app's own
    restricted background agent. Kiro Crew's own generated specs are never an
    app's agent, even when derived from one (``<app>--<agent>--readonly`` shares
    the prefix).
    """
    prefix = f"{app}--"
    try:
        agents = await asyncio.to_thread(list_agents)
        return {
            a.name
            for a in agents
            if a.filename.startswith(prefix) and not is_internal_agent_spec(a)
        }
    except Exception as exc:  # noqa: BLE001 — cannot confirm → refuse
        reason = f"cannot verify agent {agent!r} for app {app!r}: {exc}"
        _audit_spawn_denied(app, agent, reason, tool_name)
        raise SpawnError(reason) from exc


def build_spawn_impl(subagents: object) -> SpawnImpl:
    """Adapt a live ``SubagentManager`` to :data:`SpawnImpl`.

    ``approval_mode="auto"`` is REQUIRED, not incidental: the host's approval
    chain ends in "rejected" for a caller with no user to ask, so every
    unattended spawn would be declined. What bounds such a spawn is the agent's
    own tool surface, which is why an app passes its restricted background agent.

    Returns "" when the manager declines, which :meth:`SpawnSDK.run` turns into a
    raise; a refused spawn that comes back as a DONE record carrying ``error``
    raises here, because an id alone does not mean it is running.
    """

    async def _impl(
        task: str, agent: str, silent: bool, model: str, app: str, *, keep: bool = False
    ) -> str:
        if subagents is None:
            raise SpawnError("no subagent manager on this gateway")
        # The named agent must EXIST. SubagentManager validation silently replaces
        # an unknown name with "" and runs the host DEFAULT agent — full tool
        # surface, approval_mode="auto" — so a typo in an app's agent name would
        # ESCALATE from the app's restricted background agent to unrestricted
        # auto-approved execution. Fail closed: refuse a name we cannot confirm is
        # a real agent rather than fall back.
        known = await _own_agents(app, agent, "spawn_run")
        if agent not in known:
            reason = (
                f"app {app!r} may only spawn its OWN agents ({app}--*); {agent!r} is not one "
                "(refusing to run the host default or another app's agent)"
            )
            _audit_spawn_denied(app, agent, reason)
            raise SpawnError(reason)
        # An explicit per-spawn model wins; with none, the app's own model setting
        # applies unless the agent pins a model of its own.
        if not model:
            model = await asyncio.to_thread(_app_spawn_model, app, agent)
        # ``app`` is forwarded so SubagentManager.spawn can resolve the calling
        # app's per-app governance profile — a profile that denies
        # ``capabilities.spawn`` for this app must win even when the policy
        # ceiling alone would permit. Omitting it was a Level-2 (PROFILE) bypass.
        spawn_kwargs = dict(
            agent=agent,
            silent=silent,
            approval_mode="auto",
            model=model or None,
            app=app,
            keep=keep,
            # Already confirmed to exist via the off-loop list_agents() above;
            # skip the manager's synchronous re-scan on the event loop.
            _agent_prevalidated=True,
        )
        # Event-loop caller: the durable row is written on the store's writer
        # thread (``spawn_async``); a manager double without it spawns inline.
        # Shared Crew fair admission: the spawn is a child charged to the app's
        # bounded system principal; a refusal is a host decline.
        from kiro_crew.shared_crew.admission_entry import admit_app_spawn
        from kiro_crew.shared_crew.containment import principal_scope

        admission = await admit_app_spawn(subagents, app)
        if admission.rejected:
            reason = admission.reason.value if admission.reason is not None else "refused"
            raise SpawnError(
                f"fair admission refused the spawn for app {app!r} ({reason})", declined=True
            )
        from kiro_crew.shared_crew.admission_link import link_spawn

        spawn_async = getattr(subagents, "spawn_async", None)
        try:
            run_id = await link_spawn(admission, subagents)
            if run_id is None:
                raise SpawnError(
                    f"fair admission could not record the spawn for app {app!r}", declined=True
                )
            if run_id:
                spawn_kwargs["_preassigned_id"] = run_id
            with principal_scope(None if admission.work is None else admission.work.budget_key):
                if inspect.iscoroutinefunction(spawn_async):
                    info = await spawn_async(task, **spawn_kwargs)
                else:
                    info = subagents.spawn(task, **spawn_kwargs)  # type: ignore[attr-defined]
        except BaseException:
            await admission.abandon()
            raise
        if info is None or bool(info.error) or not info.id:
            await admission.abandon()
        else:
            await admission.attach_child(str(info.id), session_key=f"subagent:{info.id}")
        if info is None:
            return ""
        if getattr(info, "error", ""):
            # A done record carrying ``error`` is a refusal, not a started run.
            # A task-store refusal — the admission floor could not record the
            # spawn's durable row (a memory wait it has no row to park, or a
            # store write that failed) — carries ``error_code`` ==
            # ``task_store_unavailable`` (the gate sets it; it never sets
            # ``queued_reason`` on a refusal — that rides only on an ACCEPTED
            # queued record). Any such store refusal is the host-pressure
            # DECLINE this flags for back-off: a host that cannot record a
            # spawn gains nothing from re-attempting every one-second poll.
            # Every other error (empty/unknown agent, bad cwd, governance) is a
            # fault no cooldown fixes, raised undeclined.
            declined = str(getattr(info, "error_code", "")) == TASK_STORE_UNAVAILABLE_CODE
            raise SpawnError(str(info.error), declined=declined)
        if stopped_before_start(info):
            # Stopped while it waited: its id never runs or finishes, so handing
            # it back would leave the app polling a run that does not exist.
            raise SpawnError("the spawn was stopped before it started; nothing ran")
        return str(getattr(info, "id", "") or "")

    # Ride the probe on the impl callable rather than adding a second parameter
    # to every layer that threads spawn_impl (gateway -> lifecycle -> context).
    # build_app_context reads it back via getattr.
    _impl.done_probe = build_done_probe(subagents)  # type: ignore[attr-defined]
    _impl.continue_impl = build_continue_impl(subagents)  # type: ignore[attr-defined]
    return _impl


def _conversation_owner(subagents: object, conversation: str) -> tuple[str, str]:
    """``(app, agent)`` that started run *conversation*, from its live record or
    its protected run record. Blocking on a record that left memory: call off the
    loop. Raises :class:`_ConversationPrunedError` when the run's folder is absent
    (retention pruned it), and ``ValueError`` (or ``OSError``) when a
    record that is there cannot be read."""
    get = getattr(subagents, "get", None)
    info = get(conversation) if callable(get) else None
    if info is not None:
        return str(getattr(info, "app", "") or ""), str(getattr(info, "agent", "") or "")
    from kiro_crew.subagent_persistence import _agent_dir, read_run_execution

    try:
        execution = read_run_execution(conversation)
    except ValueError:
        # Only a folder that is provably absent is an ordinary expiry; a present
        # but unreadable record, or an id that names no folder, fails closed.
        if not _agent_dir(conversation).exists():
            raise _ConversationPrunedError(conversation) from None
        raise
    return execution.app, execution.template_id


class _ConversationPrunedError(LookupError):
    """The run's live record and its folder are both gone: nothing to resume."""


def build_continue_impl(subagents: object) -> ContinueImpl:
    """Adapt a live ``SubagentManager`` to :data:`ContinueImpl`.

    Ownership is checked before the manager is asked: the conversation's run must
    record THIS app and one of the app's own agents that still exists. The
    follow-up names that agent explicitly, so the manager's governance gate
    evaluates the app's profile and ``capabilities.spawn`` scopes against it as
    it does for :meth:`SpawnSDK.run`, and ``approval_mode="auto"`` is passed for
    the same reason as there: no user is present to approve a tool call.

    Returns "" when the manager declines for capacity; a typed refusal raises
    :class:`SpawnError` with its ``code``.
    """

    async def _continue(conversation: str, task: str, model: str, silent: bool, app: str) -> str:
        if subagents is None:
            raise SpawnError("no subagent manager on this gateway")
        try:
            run_app, agent = await asyncio.to_thread(_conversation_owner, subagents, conversation)
        except _ConversationPrunedError as exc:
            reason = f"{CONVERSATION_GONE}: conversation {conversation!r} has expired"
            _audit_spawn_denied(app, "", reason, "spawn_continue")
            raise SpawnError(reason, code=CONVERSATION_GONE) from exc
        except (OSError, ValueError) as exc:
            reason = f"cannot verify that app {app!r} owns conversation {conversation!r}: {exc}"
            _audit_spawn_denied(app, "", reason, "spawn_continue")
            raise SpawnError(reason) from exc
        if run_app != app or not agent:
            reason = (
                f"app {app!r} may only continue a run it started with one of its OWN "
                f"agents; conversation {conversation!r} is not one"
            )
            _audit_spawn_denied(app, agent, reason, "spawn_continue")
            raise SpawnError(reason)
        if agent not in await _own_agents(app, agent, "spawn_continue"):
            reason = (
                f"app {app!r} may only continue runs of its OWN agents ({app}--*); "
                f"{agent!r} is not one"
            )
            _audit_spawn_denied(app, agent, reason, "spawn_continue")
            raise SpawnError(reason)
        # A continuation runs where the run ran; the recorded path is read off
        # the loop because it may live on a slow mount.
        recorded_cwd = getattr(subagents, "recorded_cwd", None)
        cwd = await asyncio.to_thread(recorded_cwd, conversation) if callable(recorded_cwd) else ""
        from kiro_crew.shared_crew.admission_entry import admit_app_spawn
        from kiro_crew.shared_crew.admission_link import link_spawn
        from kiro_crew.shared_crew.containment import principal_scope

        admission = await admit_app_spawn(subagents, app)
        if admission.rejected:
            reason = admission.reason.value if admission.reason is not None else "refused"
            raise SpawnError(
                f"fair admission refused the continuation for app {app!r} ({reason})",
                declined=True,
            )
        continue_kwargs: dict[str, object] = dict(
            agent=agent,
            model=model or None,
            cwd=cwd,
            approval_mode="auto",
            silent=silent,
        )
        try:
            run_id = await link_spawn(admission, subagents)
            if run_id is None:
                raise SpawnError(
                    f"fair admission could not record the continuation for app {app!r}",
                    declined=True,
                )
            if run_id:
                continue_kwargs["_preassigned_id"] = run_id
            with principal_scope(None if admission.work is None else admission.work.budget_key):
                info = await subagents.continue_conversation_async(  # type: ignore[attr-defined]
                    conversation, task, **continue_kwargs
                )
        except BaseException:
            await admission.abandon()
            raise
        if info is None or bool(getattr(info, "error", "")) or not info.id:
            await admission.abandon()
        else:
            # A continuation runs on its conversation's session key, so that is
            # the key its nested spawns name.
            await admission.attach_child(
                str(info.id),
                session_key=getattr(info, "conversation_key", "") or f"subagent:{info.id}",
                also=(f"subagent:{info.id}",),
            )
        if info is None:
            return ""
        error = str(getattr(info, "error", "") or "")
        if error:
            for code in (CONVERSATION_BUSY, CONVERSATION_GONE):
                if error.startswith(code):
                    raise SpawnError(error, code=code)
            declined = str(getattr(info, "error_code", "")) == TASK_STORE_UNAVAILABLE_CODE
            raise SpawnError(error, declined=declined)
        if stopped_before_start(info):
            raise SpawnError("the continuation was stopped before it started; nothing ran")
        return str(getattr(info, "id", "") or "")

    return _continue


def build_done_probe(subagents: object) -> DoneProbe:
    """Adapt a live ``SubagentManager`` to :data:`DoneProbe`.

    An id the manager does not track reads as done: the reaper prunes
    records, and "gone" must never hold a caller's serial lock open. The one
    exception is a spawn accepted behind the concurrency / adaptive cap: it has
    no ``_agents`` entry yet (``get`` misses) but is real pending work, so
    reading it as done would clear the guard and let the caller queue a
    duplicate of work that has not run. ``is_queued`` names that window.
    """

    def _probe(spawn_id: str) -> bool:
        if subagents is None or not spawn_id:
            return True
        info = subagents.get(spawn_id)  # type: ignore[attr-defined]
        if info is not None:
            return bool(getattr(info, "done", False))
        is_queued = getattr(subagents, "is_queued", None)
        if callable(is_queued) and is_queued(spawn_id):
            return False  # accepted, not started: pending work holds the guard
        return True

    return _probe

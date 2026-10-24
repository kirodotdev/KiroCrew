"""What happens to a session's remote workspace when the session is archived.

Archive stops it and stamps ``deletion_due_at``; a reopen clears the stamp, and
:func:`sweep_due` deletes due workspaces once proven. Nothing raises into close.
Sprites and boxd sessions keep one VM each (``coder/persistent_vm.py``); their
archive, sweep and shutdown run here too, under the same per-session lifecycle
lock and generation.
An AWS Fargate task is the workspace itself, so archiving stops it at once
(:func:`_archive_fargate`), with no retention window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional
from weakref import WeakValueDictionary

from kiro_crew.coder.client import CoderClientError
from kiro_crew.coder.registry import DELETING, WorkspaceBinding, WorkspaceRegistryCorrupt
from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS

logger = logging.getLogger(__name__)

_RUNNING = "running"
#: Strong references: asyncio holds only weak ones.
_TASKS: "set[asyncio.Task[None]]" = set()
SWEEP_INTERVAL_SECS = 60.0
#: Keeps boot work off the startup path.
SWEEP_INITIAL_DELAY_SECS = 30.0


@dataclass
class SessionLifecycle:
    """Serialize one owner's workspace operations and supersede queued archives."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    generation: int = 0


# Retained only by active preparations and queued archives.
_LIFECYCLES: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], SessionLifecycle] = (
    WeakValueDictionary()
)


def session_lifecycle(session_key: str) -> SessionLifecycle:
    """Shared across provider instances on the gateway loop, keyed by workspace owner."""
    key = (asyncio.get_running_loop(), session_key)
    lifecycle = _LIFECYCLES.get(key)
    if lifecycle is None:
        lifecycle = SessionLifecycle()
        _LIFECYCLES[key] = lifecycle
    return lifecycle


def _is_recorded_workspace(found_uuid: str, recorded_uuid: str) -> bool:
    """Whether a workspace found by name is the one this binding minted.

    A binding records its workspace's UUID once that workspace ran; one that never
    did (provisioning failed) has none, and its name may since belong to another
    workspace, so nothing is stopped on its behalf.
    """
    return bool(recorded_uuid) and found_uuid == recorded_uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


async def _live_config() -> Any:
    """The live config, or a fresh load before the gateway publishes one."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return config


async def _section() -> Any:
    return (await _live_config()).session_environments


async def _persistent_environments() -> list[Any]:
    """Every VM provider's environments, so their saved rows are swept even after
    every profile of that provider is removed; none while the developer flag is
    off (no VM call then runs for any row). Read from the one provider table
    (``session_launch._vm_environments``), never listed here."""
    from kiro_crew.coder.session_launch import _vm_environments
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return []
    section = await _section()
    home = await asyncio.to_thread(data_home)
    return [cls(section, home) for cls in _vm_environments().values()]


async def _environments() -> Optional[Any]:
    """The live Coder environments, or None when Coder is not configured or the
    developer flag is off (no Coder command then runs for any binding)."""
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return None
    section = await _section()
    if not getattr(section, "coder_url", ""):
        return None
    return CoderSessionEnvironments(section, await asyncio.to_thread(data_home))


def compute_survives_stop(environments: Any, binding: WorkspaceBinding) -> bool:
    """Whether stopping keeps the files (blocking): the bound profile's flag while it
    names the template, else every profile on it must agree; none is False."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    profiles = [
        p
        for p in getattr(environments._config, "profiles", []) or []
        if getattr(p, "template", None) == binding.template
    ]
    try:
        chosen = environments.choices.get(binding.session_key)
    except SessionEnvironmentUnavailable:
        chosen = None
    matched = [p for p in profiles if p.id == chosen] or profiles
    return bool(matched) and all(getattr(p, "compute_survives_stop", True) is True for p in matched)


async def _binding_survives_stop(environments: Any, session_key: str) -> bool:
    """:func:`compute_survives_stop` for a session's binding; True when it has none."""
    binding = await asyncio.to_thread(environments.registry.get_by_session, session_key)
    if binding is None:
        return True
    return await asyncio.to_thread(compute_survives_stop, environments, binding)


async def _fargate_environments() -> Optional[Any]:
    """The live Fargate environments, or None while the developer flag is off (no
    Fargate registry read or AWS call then runs). Constructing them makes no AWS call."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.fargate_session.launch import FargateSessionEnvironments
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return None
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    return FargateSessionEnvironments(
        config.session_environments, await asyncio.to_thread(data_home)
    )


#: Bound on the pre-stop unsaved-work check; a slow machine just skips it.
UNSAVED_CHECK_SECS = 20.0
Notify = Callable[[str, str], None]


def schedule_archived(session_keys: Iterable[str], notify: Optional[Notify] = None) -> None:
    """Fire-and-forget from the archive path: the close never waits on Coder."""
    from kiro_crew.session_environment import session_environments_enabled

    keys = tuple(dict.fromkeys(k for k in session_keys if k))
    if not keys or not session_environments_enabled():
        return
    try:
        # Capture now: prepare can run before the task's first turn.
        requests = tuple((key, session_lifecycle(key)) for key in keys)
        task = asyncio.ensure_future(
            _archive(tuple((key, state, state.generation) for key, state in requests), notify)
        )
    except RuntimeError:
        return  # no running loop (a synchronous caller in a test)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def feed_notifier(state: Any) -> Optional[Notify]:
    """A :data:`Notify` that publishes to the dashboard's notification feed."""
    bus = getattr(state, "notification_bus", None)
    if bus is None:
        return None

    def notify(title: str, body: str) -> None:
        from kiro_crew.notifications.bus import NotificationPayload

        bus.push(
            NotificationPayload(
                source="system",
                channel="system.agent",
                kind="agent",
                title=title,
                body=body,
                group_key="session-environment-unsaved",
            )
        )

    return notify


async def warn_unsaved_work(
    session_keys: tuple[str, ...],
    notify: Optional[Notify],
    *,
    survives_stop: bool = True,
    ephemeral: bool = False,
) -> None:
    """Notify about unsaved work in running workspaces, before any stop; bounded, never raises.

    For an ``ephemeral`` (AWS Fargate) workspace the files go with the stop itself.
    """
    if notify is None:
        return
    from kiro_crew.coder.session_browse import unsaved_work
    from kiro_crew.session_environment import session_runs_remotely

    for key in session_keys:
        try:
            if not await asyncio.to_thread(session_runs_remotely, key):
                continue
            summary = await asyncio.wait_for(unsaved_work(key), UNSAVED_CHECK_SECS)
        except Exception:  # noqa: BLE001 -- the archive proceeds regardless
            logger.debug("Unsaved-work check failed for %s", key, exc_info=True)
            continue
        if summary:
            if ephemeral:
                body = (
                    f"Its AWS Fargate task held {summary}, which existed only in that task. "
                    "Archiving stops the task, and its files go with it."
                )
            else:
                if survives_stop:
                    advice = "Reopen the session and commit or push before its retention runs out."
                else:
                    advice = (
                        "Its files are lost when that machine's compute stops (its template's "
                        "own stop deadline or the retention deletion), so reopen the session "
                        "and commit or push now."
                    )
                body = f"Its workspace holds {summary}, which exists only on that machine. {advice}"
            try:
                notify("Archived session has unsaved remote work", body)
            except Exception:  # noqa: BLE001 -- a failed notice never fails the archive
                logger.warning("Could not publish the unsaved-work notice", exc_info=True)


async def _archive(
    requests: tuple[tuple[str, SessionLifecycle, int], ...], notify: Optional[Notify] = None
) -> None:
    try:
        environments = await _environments()
        persistent = await _persistent_environments()
        fargate = await _fargate_environments()
        for key, lifecycle, generation in requests:
            async with lifecycle.lock:
                if generation != lifecycle.generation:
                    continue
                has_task = await _fargate_task_recorded(fargate, key)
                survives = (
                    True
                    if environments is None
                    else await _binding_survives_stop(environments, key)
                )
                await warn_unsaved_work((key,), notify, survives_stop=survives, ephemeral=has_task)
                # A resume requested during the remote probe supersedes this close.
                if generation != lifecycle.generation:
                    continue
                if has_task:
                    await _archive_fargate(fargate, key)
                if environments is not None:
                    try:
                        await _archive_session(environments, key)
                    except Exception:  # noqa: BLE001 -- the other providers still run
                        logger.warning(
                            "Archived session workspace: could not schedule its release",
                            exc_info=True,
                        )
                for provider in persistent:
                    try:
                        await provider.archive_session(key)
                    except Exception:  # noqa: BLE001 -- never surfaces into the close
                        logger.warning(
                            "Archived session %s machine: could not schedule its deletion",
                            provider.label,
                            exc_info=True,
                        )
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session workspace: could not schedule its release", exc_info=True)


#: Bound on the reopen a history resume awaits before it publishes the tab.
REOPEN_SECS = 30.0


async def keep_reopened(session_keys: Iterable[str]) -> Optional[str]:
    """Awaited by a history resume BEFORE it publishes the tab: the reopened
    session keeps its remote workspace even when no message starts it (Files
    edits run on it as it is). None when every pending deletion was cancelled
    (or none was pending); else why one still stands, for a retryable refusal.
    Supersedes an archive queued before it; creates and wakes nothing."""
    from kiro_crew.session_environment import session_environments_enabled

    keys = tuple(dict.fromkeys(k for k in session_keys if k))
    if not keys or not session_environments_enabled():
        return None
    return await _keep(_claim(keys))


def _claim(keys: tuple[str, ...]) -> tuple[tuple[str, SessionLifecycle], ...]:
    """Supersede every archive queued for ``keys`` before this call (synchronous)."""
    requests = tuple((key, session_lifecycle(key)) for key in keys)
    for _key, lifecycle in requests:
        lifecycle.generation += 1
    return requests


async def _keep(requests: tuple[tuple[str, SessionLifecycle], ...]) -> Optional[str]:
    try:
        return await asyncio.wait_for(_reopen(requests), REOPEN_SECS)
    except asyncio.TimeoutError:
        logger.warning("Reopened session workspace: cancelling its deletion timed out")
        return "its remote workspace's deletion could not be cancelled in time; try again"


def schedule_reopened(session_keys: Iterable[str]) -> None:
    """Fire-and-forget :func:`keep_reopened`, for a caller with nothing to refuse."""
    from kiro_crew.session_environment import session_environments_enabled

    keys = tuple(dict.fromkeys(k for k in session_keys if k))
    if not keys or not session_environments_enabled():
        return
    try:
        task = asyncio.ensure_future(_keep_quietly(_claim(keys)))
    except RuntimeError:
        return  # no running loop (a synchronous caller in a test)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _keep_quietly(requests: tuple[tuple[str, SessionLifecycle], ...]) -> None:
    reason = await _keep(requests)
    if reason is not None:
        logger.info("Reopened session workspace: %s", reason)


async def _reopen(requests: tuple[tuple[str, SessionLifecycle], ...]) -> Optional[str]:
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    try:
        environments = await _environments()
        persistent = await _persistent_environments()
    except Exception:  # noqa: BLE001 -- no registry readable: nothing is known pending
        logger.warning("Reopened session workspace: could not read its registries", exc_info=True)
        return None
    failure: Optional[str] = None
    for key, lifecycle in requests:
        async with lifecycle.lock:
            if environments is not None:
                try:
                    await asyncio.to_thread(resume_check, environments.registry, key)
                except SessionEnvironmentUnavailable as exc:
                    # Mid-delete: the next start says so and gets a fresh one.
                    logger.info("Reopened session workspace: %s", exc)
            for provider in persistent:
                try:
                    await provider.reopen_session(key)
                except Exception as exc:  # noqa: BLE001 -- the other providers still run
                    logger.warning(
                        "Reopened session %s machine: could not keep it",
                        provider.label,
                        exc_info=True,
                    )
                    reason = str(exc) if isinstance(exc, SessionEnvironmentUnavailable) else ""
                    failure = failure or (
                        reason
                        or f"its {provider.label} machine's deletion could not be cancelled; "
                        "try again"
                    )
    return failure


async def _fargate_task_recorded(fargate: Any, session_key: str) -> bool:
    """Whether ``session_key`` has a recorded Fargate task; False when unreadable.

    The Fargate registry fails closed, but that must not stall a Coder archive:
    a task it cannot list gets no keep-alive either, so its bootstrap's idle exit
    still stops it.
    """
    if fargate is None:
        return False
    try:
        return bool(await fargate.has_task(session_key))
    except Exception:  # noqa: BLE001 -- one provider's failure never blocks another's
        logger.warning("Archived session: could not read its Fargate task", exc_info=True)
        return False


async def _archive_fargate(fargate: Any, session_key: str) -> None:
    """Stop an archived session's Fargate task now. The caller holds its lifecycle lock.

    A stopped task is gone with its files, so there is no retention window to
    honour; keeping it running would only bill for nothing. Never raises.
    """
    try:
        await fargate.archive_session(session_key)
    except Exception:  # noqa: BLE001 -- the bootstrap's idle exit still stops the task
        logger.warning("Archived session task: could not stop it", exc_info=True)


#: Ceiling on the shutdown stop. It runs beside the graceful shutdown and must end
#: inside it, so the hard exit's cleanup and audit flush still run before the
#: service manager's SIGKILL; the guest's own timeouts stop what it misses (a
#: sprite goes cold, a boxd machine hibernates).
SHUTDOWN_STOP_SECS = GRACEFUL_SHUTDOWN_SECS * 0.8
#: boxd machines hibernated at once on shutdown (each is two CLI calls).
_SHUTDOWN_HIBERNATE_PARALLEL = 8


async def stop_on_shutdown_enabled() -> bool:
    """``session_environments.stop_on_shutdown`` (default True); True when unreadable."""
    try:
        return bool(getattr(await _section(), "stop_on_shutdown", True))
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the safe default
        return True


async def close_fargate_forwards() -> None:
    """End this gateway's own Fargate SSM forward children; never touches a task.

    Runs on every shutdown, whatever ``stop_on_shutdown`` says: the switch is
    about the tasks, and these are the gateway's processes, which would
    otherwise outlive it holding their loopback ports. A gateway that never
    loaded the Fargate launcher opened no forward, so shutdown does not load it.
    """
    launch = sys.modules.get("kiro_crew.fargate_session.launch")
    if launch is None:
        return
    await launch.close_forwards()


async def stop_for_shutdown() -> dict[str, int]:
    """Stop every owned remote environment, stamping nothing so a reopen resumes;
    stop-destroys-files ones and every Fargate task (its row and secret survive
    for reconnect) count as ``skipped``, and a boxd machine is hibernated. A
    sprite needs no call: it idles by itself, with no compute charge and its
    filesystem kept (https://docs.sprites.dev, "Automatic Idle Behavior"). Never
    raises."""
    counts = {"coder": 0, "boxd": 0, "skipped": 0}

    async def coder() -> None:
        environments = await _environments()
        if environments is None:
            return
        rows = await asyncio.to_thread(environments.registry.list_bindings)
        if not rows:
            return
        client = environments._client()
        for row in rows:
            if row.state == DELETING or row.deployment_id != environments.url:
                continue
            if not await asyncio.to_thread(compute_survives_stop, environments, row):
                counts["skipped"] += 1
                continue
            try:
                ws = await client.get_workspace(row.workspace_name)
                if (
                    ws is not None
                    and ws.status == _RUNNING
                    and _is_recorded_workspace(ws.uuid, row.workspace_uuid)
                ):
                    await client.stop_workspace(row.workspace_name)
                    counts["coder"] += 1
            except CoderClientError as exc:
                logger.info("Shutdown: Coder workspace stop failed (%s); auto-stop applies", exc)

    async def boxd() -> None:
        from kiro_crew.boxd_session.launch import BoxdSessionEnvironments
        from kiro_crew.config.paths import data_home

        environments = BoxdSessionEnvironments(await _section(), await asyncio.to_thread(data_home))
        rows = await asyncio.to_thread(environments.registry.all)
        if not rows:
            return  # no CLI call, so a gateway without boxd pays nothing
        cli = await environments.cli()
        # In parallel, a few at a time: one by one, the later machines would
        # outlast the shutdown budget and stay running.
        gate = asyncio.Semaphore(_SHUTDOWN_HIBERNATE_PARALLEL)

        async def hibernate(row: Any) -> None:
            async with gate:
                try:
                    record = await cli.get(row.name)
                    if record is None:
                        return
                    environments.check_identity(row, record)
                    await cli.hibernate(row.name)
                    counts["boxd"] += 1
                except Exception as exc:  # noqa: BLE001 -- boxd idles it on its own
                    logger.info("Shutdown: boxd hibernate failed (%s); boxd idles it", exc)

        await asyncio.gather(*(hibernate(row) for row in rows))

    async def fargate() -> None:
        try:
            environments = await _fargate_environments()
            if environments is None:
                return
            # The task IS the workspace: shutdown must not destroy its files.
            # A registry that cannot be read raises here (it fails closed).
            live_tasks = len(await asyncio.to_thread(environments.registry.all))
            # Read before adding: "+=" would load the count before the await and
            # lose a concurrent provider's increment.
            counts["skipped"] += live_tasks
        finally:
            # Its SSM forwards are this gateway's own processes, though: end
            # them whether or not the registry could be counted.
            await close_fargate_forwards()

    async def guarded(name: str, step: Callable[[], Any]) -> None:
        try:
            await step()
        except Exception:  # noqa: BLE001 -- shutdown must not fail on one provider
            logger.warning("Shutdown: could not stop %s session environments", name, exc_info=True)

    await asyncio.gather(
        guarded("Coder", coder), guarded("boxd", boxd), guarded("Fargate", fargate)
    )
    if counts["skipped"]:
        logger.info(
            "Shutdown: left %d session environment(s) running: stopping their compute "
            "would destroy their files",
            counts["skipped"],
        )
    if counts["coder"] or counts["boxd"]:
        logger.info("Shutdown: stopped session environments %s", counts)
    return counts


async def archive_session(environments: Any, session_key: str) -> Optional[WorkspaceBinding]:
    """Stamp ``session_key``'s workspace for deletion and stop it. None if it has none."""
    lifecycle = session_lifecycle(session_key)
    async with lifecycle.lock:
        return await _archive_session(environments, session_key)


async def _archive_session(environments: Any, session_key: str) -> Optional[WorkspaceBinding]:
    from kiro_crew.config.session_environments import ARCHIVED_RETENTION_DAYS

    existing = await asyncio.to_thread(environments.registry.get_by_session, session_key)
    if existing is None or existing.deployment_id != environments.url:
        return None

    due = _now() + timedelta(days=ARCHIVED_RETENTION_DAYS)
    binding = await asyncio.to_thread(
        environments.registry.schedule_deletion, session_key, _stamp(due)
    )
    if binding is None or binding.deployment_id != environments.url:
        return None
    if not await asyncio.to_thread(compute_survives_stop, environments, binding):
        # A stop would destroy the files retention exists to keep.
        logger.info("Archived session workspace: not stopped; its stop destroys its files")
        return binding
    try:
        workspace = await environments._client().get_workspace(binding.workspace_name)
        if (
            workspace is not None
            and workspace.status == _RUNNING
            and _is_recorded_workspace(workspace.uuid, binding.workspace_uuid)
        ):
            await environments._client().stop_workspace(binding.workspace_name)
    except CoderClientError as exc:
        # Auto-stop still applies; the stamp stands.
        logger.info("Archived session workspace: stop failed (%s); auto-stop applies", exc)
    return binding


async def sweep_due(environments: Any, *, now: Optional[datetime] = None) -> int:
    """Delete every workspace whose retention ran out. Returns how many went."""
    moment = now or _now()
    rows = await asyncio.to_thread(environments.registry.due_for_deletion, moment)
    rows = [row for row in rows if row.deployment_id == environments.url]
    if not rows:
        return 0
    client = environments._client()
    owner, owner_id = await client.current_user()
    deleted = 0
    for row in rows:
        claimed = await asyncio.to_thread(
            environments.registry.begin_deletion, row.binding_id, moment
        )
        if claimed is None or claimed.deployment_id != environments.url:
            continue  # reopened in the meantime
        try:
            workspace = await client.get_workspace(claimed.workspace_name)
        except CoderClientError as exc:
            logger.warning("Archived session workspace: lookup failed (%s); retrying later", exc)
            continue
        if workspace is not None:
            if (
                workspace.owner not in (owner, owner_id)
                or workspace.template != claimed.template
                or not _is_recorded_workspace(workspace.uuid, claimed.workspace_uuid)
            ):
                # Not the workspace this row minted: forget the row, never act on it.
                logger.warning("Archived session workspace changed identity; forgetting it")
            else:
                try:
                    await client.delete_workspace(claimed.workspace_name)
                except CoderClientError as exc:
                    # Stays ``deleting``: retried next pass, reopen refused.
                    logger.warning("Archived session workspace: delete failed (%s)", exc)
                    continue
        await asyncio.to_thread(environments.registry.remove_deleted, claimed.binding_id)
        deleted += 1
    return deleted


#: provider label -> the last sweep failure logged for it, so the same failure
#: every minute is one warning, not sixty.
_last_vm_sweep_error: dict[str, str] = {}


async def _sweep_persistent() -> int:
    deleted = 0
    for provider in await _persistent_environments():
        try:
            deleted += await provider.sweep_due()
            _last_vm_sweep_error.pop(provider.label, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one provider never stops the others
            message = str(exc)
            if _last_vm_sweep_error.get(provider.label) != message:
                logger.warning("Archived session %s sweep failed: %s", provider.label, message)
            _last_vm_sweep_error[provider.label] = message
    return deleted


async def keep_alive_once() -> int:
    """Touch every live Fargate task once; a failure is logged, never raised."""
    try:
        fargate = await _fargate_environments()
        if fargate is None:
            return 0
        return int(await fargate.keep_alive())
    except Exception:  # noqa: BLE001 -- the heartbeat outlives any one pass
        logger.warning("Retention sweep: Fargate keep-alive failed", exc_info=True)
        return 0


async def sweep_once(*, keep_alive: bool = True) -> int:
    """One sweep on the live config: keep live Fargate tasks alive and retry their
    stops, then Sprites and boxd VMs, then Coder (0 without it); registry-only
    unless a row is due, so an idle gateway makes no remote call.

    A Fargate failure (its registry fails closed) never stops another provider.
    ``keep_alive=False`` leaves the heartbeat to :func:`run_sweeper`'s own loop.
    """
    if keep_alive:
        await keep_alive_once()
    stopped = 0
    try:
        fargate = await _fargate_environments()
        if fargate is not None:
            stopped = await fargate.retry_stops()
    except Exception:  # noqa: BLE001 -- one provider's failure never blocks another's
        logger.warning("Retention sweep: Fargate session tasks failed", exc_info=True)
    deleted = stopped + await _sweep_persistent()
    environments = await _environments()
    if environments is None:
        return deleted
    return deleted + await sweep_due(environments)


async def _keep_alive_loop(interval: float) -> None:
    """The Fargate heartbeat, on its own clock.

    Slow stop retries or a slow Coder sweep must never delay it: a live task
    that goes 900 s without a touch idle-exits and deletes its files.
    """
    while True:
        await keep_alive_once()
        await asyncio.sleep(interval)


async def run_sweeper(
    *,
    interval: float = SWEEP_INTERVAL_SECS,
    initial_delay: float = SWEEP_INITIAL_DELAY_SECS,
) -> None:
    """Run retention sweeps until cancelled. Failures are logged; the loop never dies."""
    await asyncio.sleep(initial_delay)
    heartbeat = asyncio.create_task(_keep_alive_loop(interval))
    try:
        await _sweep_forever(interval)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat


async def _sweep_forever(interval: float) -> None:
    last_error = ""
    while True:
        try:
            deleted = await sweep_once(keep_alive=False)
            if deleted:
                logger.info("Archived session workspaces deleted: %d", deleted)
            last_error = ""
        except asyncio.CancelledError:
            raise
        except (CoderClientError, WorkspaceRegistryCorrupt) as exc:
            # The same failure every minute is one warning, not sixty.
            message = str(exc)
            if message != last_error:
                logger.warning("Archived session sweep failed: %s", message)
            last_error = message
        except Exception:  # noqa: BLE001 -- the loop outlives any one pass
            logger.warning("Archived session sweep failed", exc_info=True)
        await asyncio.sleep(interval)


def resume_check(registry: Any, session_key: str) -> None:
    """Cancel a pending deletion for a reopened session, or refuse mid-delete."""
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    binding = registry.get_by_session(session_key)
    if binding is None:
        return
    if binding.state == DELETING or (
        binding.deletion_due_at and registry.cancel_deletion(binding.binding_id) is None
    ):
        raise SessionEnvironmentUnavailable(
            "this archived session's workspace is being deleted; try again in a minute "
            "and it will start on a fresh one"
        )

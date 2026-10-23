"""What happens to a session's Coder workspace when the session is archived.

Archive stops it and stamps ``deletion_due_at``; a reopen clears the stamp, and
:func:`sweep_due` deletes due workspaces once proven. Nothing raises into close.
"""

from __future__ import annotations

import asyncio
import logging
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


async def _environments() -> Optional[Any]:
    """The live Coder environments, or None when Coder is not configured or the
    developer flag is off (no Coder command then runs for any binding)."""
    from kiro_crew.coder.session_launch import CoderSessionEnvironments
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled():
        return None
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
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
    session_keys: tuple[str, ...], notify: Optional[Notify], *, survives_stop: bool = True
) -> None:
    """Notify about unsaved work in running workspaces, before any stop; bounded, never raises."""
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
            try:
                if survives_stop:
                    advice = "Reopen the session and commit or push before its retention runs out."
                else:
                    advice = (
                        "Its files are lost when that machine's compute stops (its template's "
                        "own stop deadline or the retention deletion), so reopen the session "
                        "and commit or push now."
                    )
                notify(
                    "Archived session has unsaved remote work",
                    f"Its workspace holds {summary}, which exists only on that machine. {advice}",
                )
            except Exception:  # noqa: BLE001 -- a failed notice never fails the archive
                logger.warning("Could not publish the unsaved-work notice", exc_info=True)


async def _archive(
    requests: tuple[tuple[str, SessionLifecycle, int], ...], notify: Optional[Notify] = None
) -> None:
    try:
        environments = await _environments()
        if environments is None:
            return
        for key, lifecycle, generation in requests:
            async with lifecycle.lock:
                if generation != lifecycle.generation:
                    continue
                survives = await _binding_survives_stop(environments, key)
                await warn_unsaved_work((key,), notify, survives_stop=survives)
                # A resume requested during the remote probe supersedes this close.
                if generation == lifecycle.generation:
                    await _archive_session(environments, key)
    except Exception:  # noqa: BLE001 -- never surfaces into a close that already succeeded
        logger.warning("Archived session workspace: could not schedule its release", exc_info=True)


#: Ceiling on the shutdown stop. It runs beside the graceful shutdown and must end
#: inside it, so the hard exit's cleanup and audit flush still run before the
#: service manager's SIGKILL; the guest's own timeouts stop what it misses.
SHUTDOWN_STOP_SECS = GRACEFUL_SHUTDOWN_SECS * 0.8


async def stop_on_shutdown_enabled() -> bool:
    """``session_environments.stop_on_shutdown`` (default True); True when unreadable."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        config = live.snapshot()
        if config is None:
            config = await asyncio.to_thread(KiroCrewConfig.load)
        return bool(getattr(config.session_environments, "stop_on_shutdown", True))
    except Exception:  # noqa: BLE001 -- an unreadable config keeps the safe default
        return True


async def stop_for_shutdown() -> dict[str, int]:
    """Stop every owned remote environment, stamping nothing so a reopen resumes;
    stop-destroys-files ones count as ``skipped``. Never raises."""
    counts = {"coder": 0, "skipped": 0}

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

    try:
        await coder()
    except Exception:  # noqa: BLE001 -- shutdown must not fail on one provider
        logger.warning("Shutdown: could not stop Coder session environments", exc_info=True)
    if counts["skipped"]:
        logger.info(
            "Shutdown: left %d session environment(s) running: stopping their compute "
            "would destroy their files",
            counts["skipped"],
        )
    if counts["coder"]:
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


async def sweep_once() -> int:
    """One sweep on the live config (0 without Coder); no Coder command unless a row is due."""
    environments = await _environments()
    if environments is None:
        return 0
    return await sweep_due(environments)


async def run_sweeper(
    *,
    interval: float = SWEEP_INTERVAL_SECS,
    initial_delay: float = SWEEP_INITIAL_DELAY_SECS,
) -> None:
    """Run retention sweeps until cancelled. Failures are logged; the loop never dies."""
    await asyncio.sleep(initial_delay)
    last_error = ""
    while True:
        try:
            deleted = await sweep_once()
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

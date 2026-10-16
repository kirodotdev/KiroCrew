"""Prepare a session's Fargate task: the same seam :class:`CoderSessionEnvironments` offers.

A Fargate workspace is ephemeral: the task IS the workspace, and stopping it
deletes it. So a session keeps its task while it lives, an archived session's
task is stopped at once (there is nothing to keep it for), and a session reopened
after its task is gone gets a fresh one. The chat history, memory and approvals
stay on the gateway as always; only files in the task are lost.

Security properties:

* A gateway-only vault MAC binds each recorded task to its session and launch
  profile. Reuse requires a running task with that session's live AWS tag;
  stopping also requires the tag, so a foreign task is never acted on.
* The session secret is generated here, delivered over a kernel-verified SSM
  forward by one-shot enrolment, and kept in the gateway's encrypted vault so a gateway
  restart can reach the same task. It is never in a task definition, an
  override, an environment variable, a log, or the agent's reach.
* Every answer from the task is HMAC-checked with that secret, so only the task
  this gateway enrolled can answer at all: that is the live identity proof each
  later call relies on.
* A task whose enrolment this gateway did not win is stopped, never used.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from kiro_crew.atomic_write import atomic_write
from kiro_crew.coder.session_launch import PreparedRemoteSession
from kiro_crew.fargate_session import bootstrap, tasks
from kiro_crew.fargate_session.forward import PortForward
from kiro_crew.fargate_session.shell import FargateShell, FargateWorkerLaunch
from kiro_crew.platform_compat import file_lock
from kiro_crew.secrets.vault import SecretVault
from kiro_crew.session_environment import SessionEnvironmentUnavailable, report_start_stage

PROVIDER_ID = "fargate"
logger = logging.getLogger(__name__)
_REGISTRY_VERSION = 1
_ENROLL_ATTEMPTS = 20
_ENROLL_GAP_SECS = 3.0
_KEEPALIVE_CONCURRENCY = 4
_KEEPALIVE_TIMEOUT_SECS = 30.0
#: One forward per task, shared by every runtime and relay of that session.
_FORWARDS: dict[str, PortForward] = {}
_T = TypeVar("_T")


@dataclass(frozen=True)
class SessionTask:
    session_key: str
    profile_id: str
    arn: str
    task_id: str
    cluster: str
    runtime_id: str = ""
    stop_reason: str = ""
    aws_profile: str = ""
    binding_mac: str = ""

    @property
    def task(self) -> tasks.FargateTask:
        return tasks.FargateTask(self.arn, self.task_id, self.cluster, self.runtime_id)

    @property
    def worker_binding(self) -> str:
        """The guest worker binding id: one per task, shared by every runtime."""
        return f"fg-{self.task_id[:24]}"


class SessionTaskRegistry:
    """``session key -> SessionTask``, written atomically under an exclusive lock.

    Blocking file I/O: callers on the event loop go through ``asyncio.to_thread``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self) -> dict[str, SessionTask]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SessionEnvironmentUnavailable(
                "the Fargate session registry is unreadable"
            ) from exc
        try:
            raw = json.loads(text)
            if raw.get("version") != _REGISTRY_VERSION:
                raise ValueError("unknown registry version")
            rows = {key: SessionTask(**value) for key, value in raw["tasks"].items()}
            if any(key != row.session_key for key, row in rows.items()):
                raise ValueError("task stored under another session")
            return rows
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # Fail closed: an unreadable registry must not forget a billed task.
            raise SessionEnvironmentUnavailable(
                "the Fargate session registry is unreadable"
            ) from exc

    def _write(self, rows: dict[str, SessionTask]) -> None:
        payload = {
            "version": _REGISTRY_VERSION,
            "tasks": {k: asdict(v) for k, v in sorted(rows.items())},
        }
        atomic_write(
            self.path, json.dumps(payload, indent=2) + "\n", fsync=True, restrict_to_owner=True
        )

    def _locked(self, mutate: Callable[[dict[str, SessionTask]], _T]) -> _T:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as handle:
            with file_lock(handle.fileno(), exclusive=True, required=True):
                rows = self._read()
                result = mutate(rows)
                self._write(rows)
                return result

    def get(self, session_key: str) -> Optional[SessionTask]:
        return self._read().get(session_key)

    def all(self) -> list[SessionTask]:
        return list(self._read().values())

    def put(self, row: SessionTask) -> None:
        self._locked(lambda rows: rows.__setitem__(row.session_key, row))

    def remove(self, session_key: str, arn: str) -> bool:
        """Drop the row only if it still names ``arn`` (a newer task is left alone)."""

        def mutate(rows: dict[str, SessionTask]) -> bool:
            current = rows.get(session_key)
            if current is None or current.arn != arn:
                return False
            del rows[session_key]
            return True

        return self._locked(mutate)

    def mark_stopping(self, row: SessionTask, stopping: SessionTask) -> bool:
        """Persist a signed stop only if the exact verified snapshot is still current."""

        def mutate(rows: dict[str, SessionTask]) -> bool:
            if rows.get(row.session_key) != row:
                return False
            rows[row.session_key] = stopping
            return True

        return self._locked(mutate)


class FargateSessionEnvironments:
    """Run, reuse and stop Fargate session tasks for the saved Fargate profiles."""

    provider_id = PROVIDER_ID

    def __init__(self, config: Any, data_home: Path) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self.registry = SessionTaskRegistry(self.data_home / "fargate" / "session-tasks.json")
        self._secrets = SecretVault(self.data_home)
        self.worker_secrets_dir = self.data_home / "remote-session-secrets"

    def profiles(self) -> list[dict[str, Any]]:
        return [
            p
            for p in (getattr(self._config, "fargate_profiles", None) or [])
            if isinstance(p, dict)
        ]

    def profile(self, profile_id: str) -> dict[str, Any]:
        for p in self.profiles():
            if p.get("id") == profile_id:
                return p
        raise SessionEnvironmentUnavailable(
            f"session environment profile {profile_id!r} is not configured"
        )

    # -- persisted identity ----------------------------------------------------------
    def _binding_mac(self, row: SessionTask) -> str:
        """Authenticate identity and stop intent with a key never shared with the guest."""
        key = self._secrets.derive_subkey("fargate-session-binding-v1")
        fields = [
            row.session_key,
            row.profile_id,
            row.arn,
            row.task_id,
            row.cluster,
            row.runtime_id,
            row.aws_profile,
        ]
        # Active rows keep the identity-only encoding; stop intent extends it.
        if row.stop_reason:
            fields.append(row.stop_reason)
        payload = json.dumps(fields, separators=(",", ":")).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def _record_task(self, row: SessionTask, profile: dict[str, Any]) -> SessionTask:
        """Sign a just-launched task and persist it; never sign a row read from disk."""
        row = replace(row, aws_profile=str(profile.get("aws_profile") or ""))
        row = replace(row, binding_mac=self._binding_mac(row))
        self.registry.put(row)
        return row

    def _record_stop(self, row: SessionTask, reason: str) -> bool:
        """Sign an authorized transition, never unverified registry contents."""
        self._verify_binding(row)
        stopping = replace(row, stop_reason=reason)
        stopping = replace(stopping, binding_mac=self._binding_mac(stopping))
        return self.registry.mark_stopping(row, stopping)

    def _verify_binding(self, row: SessionTask) -> None:
        try:
            if row.binding_mac and hmac.compare_digest(row.binding_mac, self._binding_mac(row)):
                return
        except (OSError, ValueError, TypeError):
            pass
        raise SessionEnvironmentUnavailable(
            "refusing the Fargate task: its recorded session/profile binding is not authenticated"
        )

    @staticmethod
    def _task_profile(profile: dict[str, Any], row: SessionTask) -> dict[str, Any]:
        """Use an authenticated task's launch credentials and ARN region."""
        return {**profile, "aws_profile": row.aws_profile, "region": _arn_region(row.arn)}

    # -- secrets --------------------------------------------------------------------
    @staticmethod
    def _secret_name(task_id: str) -> str:
        if not task_id.isalnum():
            raise SessionEnvironmentUnavailable("unexpected ECS task id")
        return f"FARGATE_BOOTSTRAP_{task_id}"

    def _save_secret(self, task_id: str, secret: bytes) -> None:
        self._secrets.set_sync(self._secret_name(task_id), secret.hex())

    def load_secret(self, task_id: str) -> Optional[bytes]:
        """The enrolled secret for ``task_id``, or None when none is in the vault."""
        try:
            value = self._secrets.get(self._secret_name(task_id))
            return bytes.fromhex(value.reveal()) if value is not None else None
        except (OSError, ValueError):
            return None

    def _forget_secret(self, task_id: str) -> None:
        with contextlib.suppress(OSError, SessionEnvironmentUnavailable):
            self._secrets.delete_sync(self._secret_name(task_id))

    # -- forwards -------------------------------------------------------------------
    def forward(self, profile: dict[str, Any], task: tasks.FargateTask) -> PortForward:
        """The shared SSM forward to ``task``'s bootstrap, created on first use."""
        forward = _FORWARDS.get(task.task_id)
        if forward is None or forward.target != task.ssm_target:
            forward = PortForward(
                task.ssm_target,
                bootstrap.BOOTSTRAP_PORT,
                profile=str(profile.get("aws_profile") or ""),
                region=str(profile.get("region") or ""),
            )
            _FORWARDS[task.task_id] = forward
        return forward

    async def _drop_forward(self, task_id: str) -> None:
        forward = _FORWARDS.pop(task_id, None)
        if forward is not None:
            await forward.stop()

    # -- lifecycle ------------------------------------------------------------------
    async def _reusable(self, profile: dict[str, Any], row: SessionTask) -> Optional[bytes]:
        """The secret for ``row``'s task if it is still ours and running, else None."""
        await asyncio.to_thread(self._verify_binding, row)
        secret = await asyncio.to_thread(self.load_secret, row.task_id)
        if (
            secret is None
            or not row.runtime_id
            or row.stop_reason
            or row.profile_id != profile["id"]
        ):
            return None
        info = await tasks.describe(self._task_profile(profile, row), row.task)
        if info is None or info.get("lastStatus") != "RUNNING":
            return None
        if info.get("taskArn") != row.arn or not tasks.carries_session_tag(info, row.session_key):
            return None
        return secret

    async def _launch(self, session_key: str, profile: dict[str, Any]) -> tuple[SessionTask, bytes]:
        report_start_stage(session_key, "provisioning")
        container = await tasks.task_container(profile)
        task = await tasks.run_task(
            profile,
            session_key,
            container=container,
            command=bootstrap.container_command(),
            environment={
                "KIROCREW_WORKDIR": str(profile.get("workdir") or ""),
                "KIROCREW_BOOTSTRAP_PORT": str(bootstrap.BOOTSTRAP_PORT),
            },
        )
        row = SessionTask(session_key, str(profile["id"]), task.arn, task.task_id, task.cluster)
        # Recorded before waiting, so an archive (or a failure below) can stop it.
        row = await asyncio.to_thread(self._record_task, row, profile)
        try:
            running = await tasks.wait_running(profile, task)
            report_start_stage(session_key, "connecting")
            secret = os.urandom(32)
            await self._enroll(self.forward(profile, running), secret)
            await asyncio.to_thread(self._save_secret, running.task_id, secret)
            row = await asyncio.to_thread(
                self._record_task, replace(row, runtime_id=running.runtime_id), profile
            )
            return row, secret
        except BaseException:
            await self._stop(profile, row, "the session task did not start cleanly")
            raise

    async def _enroll(self, forward: PortForward, secret: bytes) -> None:
        # The task reports RUNNING a few seconds before the bootstrap listens.
        last: Optional[BaseException] = None
        for _ in range(_ENROLL_ATTEMPTS):
            try:
                await bootstrap.enroll(forward.connect, secret, verify_peer=forward.verify_peer)
                return
            except bootstrap.AlreadyEnrolled as exc:
                raise SessionEnvironmentUnavailable(
                    "someone else reached the session task first; it was stopped"
                ) from exc
            except (bootstrap.BootstrapError, SessionEnvironmentUnavailable) as exc:
                last = exc
            await asyncio.sleep(_ENROLL_GAP_SECS)
        raise SessionEnvironmentUnavailable(f"the session task's bootstrap never answered: {last}")

    async def _stop(self, profile: dict[str, Any], row: SessionTask, reason: str) -> None:
        if not await asyncio.to_thread(self._record_stop, row, reason):
            return
        profile = self._task_profile(profile, row)
        info = await tasks.describe(profile, row.task)
        if info is not None and (
            info.get("taskArn") != row.arn or not tasks.carries_session_tag(info, row.session_key)
        ):
            logger.warning(
                "Fargate task %s changed live identity or session tag; forgetting its binding "
                "without stopping it",
                row.arn,
            )
            info = None
        await self._drop_forward(row.task_id)
        # Failed or cancelled stops keep the durable intent for the sweeper.
        if info is not None:
            try:
                await tasks.stop_task(profile, row.task, reason)
            except SessionEnvironmentUnavailable:
                # ECS can purge an exited task between the describe and stop calls.
                if await tasks.describe(profile, row.task) is not None:
                    raise
        await asyncio.to_thread(self._forget_secret, row.task_id)
        await asyncio.to_thread(self.registry.remove, row.session_key, row.arn)

    async def prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        """Reuse this session's running task, or launch, enrol and record a new one."""
        from kiro_crew.coder.retention import session_lifecycle

        lifecycle = session_lifecycle(session_key)
        # Invalidate queued archives, then wait for one already stopping the
        # task: a resume must never adopt a task an archive is about to stop.
        lifecycle.generation += 1
        async with lifecycle.lock:
            return await self._prepare(session_key, profile_id)

    async def _prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        profile = self.profile(profile_id)
        row = await asyncio.to_thread(self.registry.get, session_key)
        secret: Optional[bytes] = None
        if row is not None:
            secret = await self._reusable(profile, row)
            if secret is None:
                # Only an authenticated task with the matching live tag may be stopped.
                await self._stop(profile, row, "replaced: no longer reachable")
                row = None
        created = False
        if row is None or secret is None:
            created = True
            row, secret = await self._launch(session_key, profile)
        shell = FargateShell(
            self.forward(self._task_profile(profile, row), row.task).connect, secret
        )
        shell.workspace = f"task {row.task_id[:12]}"
        launch = FargateWorkerLaunch(row.worker_binding, shell, self.worker_secrets_dir)
        return PreparedRemoteSession(
            launch,
            str(profile.get("workdir") or ""),
            shell.workspace,
            str(profile["id"]),
            str(profile.get("name") or profile["id"]),
            created=created,
        )

    async def shell_for(self, session_key: str, profile_id: str) -> Optional[FargateShell]:
        """A shell into ``session_key``'s enrolled task, or None. Never launches one.

        No AWS call: the forward targets the authenticated binding, and the HMAC
        on every answer proves it is the task this gateway enrolled.
        """
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None or not row.runtime_id or row.profile_id != profile_id:
            return None
        if row.stop_reason:
            try:
                await asyncio.to_thread(self._verify_binding, row)
            except SessionEnvironmentUnavailable:
                # A forged reason on an authenticated active row must not suppress
                # keepalives and let the idle exit destroy that task's files.
                row = replace(row, stop_reason="")
            else:
                return None
        await asyncio.to_thread(self._verify_binding, row)
        secret = await asyncio.to_thread(self.load_secret, row.task_id)
        if secret is None:
            return None
        shell = FargateShell(self.forward(self._task_profile({}, row), row.task).connect, secret)
        shell.workspace = f"task {row.task_id[:12]}"
        return shell

    async def has_task(self, session_key: str) -> bool:
        """Whether a task is recorded for ``session_key``. Registry only."""
        return await asyncio.to_thread(self.registry.get, session_key) is not None

    async def keep_alive(self) -> int:
        """Touch enrolled tasks even without a worker, with bounded parallel probes.

        The bootstrap's idle exit bounds orphan billing, not session retention:
        a live gateway must keep unarchived task files alive between turns.
        """
        from kiro_crew.coder.retention import session_lifecycle

        rows = await asyncio.to_thread(self.registry.all)
        pending = iter(row for row in rows if row.runtime_id)
        touched = 0

        async def touch(row: SessionTask) -> bool:
            lifecycle = session_lifecycle(row.session_key)
            async with lifecycle.lock:
                shell = await self.shell_for(row.session_key, row.profile_id)
                if shell is None:
                    return False
                await shell.run("true")
                return True

        async def worker() -> None:
            nonlocal touched
            for row in pending:
                try:
                    if await asyncio.wait_for(touch(row), _KEEPALIVE_TIMEOUT_SECS):
                        touched += 1
                except Exception:  # noqa: BLE001 -- one unreachable task cannot starve the others
                    logger.warning(
                        "Fargate keepalive failed for %s", row.session_key, exc_info=True
                    )

        await asyncio.gather(*(worker() for _ in range(min(len(rows), _KEEPALIVE_CONCURRENCY))))
        return touched

    async def retry_stops(self) -> int:
        """Retry recorded stops under each session's archive/resume lifecycle lock."""
        from kiro_crew.coder.retention import session_lifecycle

        stopped = 0
        for candidate in await asyncio.to_thread(self.registry.all):
            if not candidate.stop_reason:
                continue
            lifecycle = session_lifecycle(candidate.session_key)
            async with lifecycle.lock:
                row = await asyncio.to_thread(self.registry.get, candidate.session_key)
                if row is None or row.arn != candidate.arn or not row.stop_reason:
                    continue
                try:
                    # Consume only this snapshot's authenticated intent. A second
                    # registry read must not lend its identity to an unchecked reason.
                    await self._stop({}, row, row.stop_reason)
                except SessionEnvironmentUnavailable:
                    continue
                stopped += 1
        return stopped

    async def archive_session(
        self, session_key: str, reason: str = "the session was archived"
    ) -> bool:
        """Stop a session's task now: an ephemeral workspace has nothing to keep.

        The caller holds the session's lifecycle lock (see ``coder.retention``).
        """
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return False
        # The authenticated row holds the launch credentials even if its profile is removed.
        await self._stop({}, row, reason)
        return True


def _arn_region(arn: str) -> str:
    """The region field of an ARN (``arn:aws:ecs:<region>:...``), or ""."""
    parts = arn.split(":")
    return parts[3] if len(parts) > 3 else ""

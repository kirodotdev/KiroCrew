"""Turn a saved Coder profile into a running remote session for one Crew session.

Two pieces:

* :class:`SessionEnvironmentChoices` records which profile, if any, a Crew
  session runs on. It is the only thing that makes a session remote: a session
  with no record runs locally exactly as before.
* :class:`CoderSessionEnvironments` prepares one: it reserves the session's
  workspace in the ownership registry BEFORE any compute exists, finds or creates
  and starts that workspace with the gateway's own ``coder login``, proves the
  workspace is the one the registry minted (owner, template, uuid), and returns a
  :class:`~kiro_crew.coder.remote_worker.RemoteWorkerLaunch` plus the guest
  working directory for ``AcpRuntime``.

Security properties:

* The gateway never holds a Coder token here. Commands run the ``coder`` CLI with
  its own stored login, and no ``CODER_SESSION_TOKEN`` is set unless the caller
  passes one explicitly.
* A workspace is adopted only when the registry row for THIS session named it,
  and only while its owner is the signed-in user and its template is the
  profile's. A name that later points at another uuid is refused, never adopted.
* Creating a workspace sets an auto-stop, so abandoned compute stops billing even
  if the gateway disappears.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.coder.client import CoderClient, CoderClientError
from kiro_crew.coder.registry import WorkspaceBindingRegistry
from kiro_crew.coder.remote_worker import (
    CoderShell,
    RemoteWorkerLaunch,
)
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_environment import SessionEnvironmentUnavailable, report_start_stage

logger = logging.getLogger(__name__)

PROVIDER_ID = "coder"
WORKSPACE_PREFIX = "crewenv"
DEFAULT_STOP_AFTER_MINUTES = 60
_CHOICES_VERSION = 1
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RUNNING = "running"
_STARTABLE = frozenset({"stopped", "failed", "canceled"})
_MAX_SESSION_KEY = 512


class SessionEnvironmentChoices:
    """Durable session -> profile choices. Atomic, owner-only, lock-serialized."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")

    @staticmethod
    def _provider(profile_id: str) -> str:
        from kiro_crew.config import live
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.session_environments import provider_for_profile

        config = live.snapshot() or KiroCrewConfig.load()
        # An unavailable profile may still be recorded; launch reports its absence.
        return provider_for_profile(config.session_environments, profile_id) or PROVIDER_ID

    def _read(self) -> dict[str, dict[str, str]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # No file means no session was ever bound: every session is local.
            # (Read directly rather than exists()-then-read, which races.)
            return {}
        except OSError as exc:
            raise SessionEnvironmentUnavailable(
                "session environment choices are unreadable"
            ) from exc
        try:
            raw = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Fail closed: an unreadable record must not silently make a remote
            # session local (or the reverse).
            raise SessionEnvironmentUnavailable(
                "session environment choices are unreadable"
            ) from exc
        if not isinstance(raw, dict):
            raise SessionEnvironmentUnavailable("session environment choices are invalid")
        sessions = raw.get("sessions")
        if raw.get("version") != _CHOICES_VERSION or not isinstance(sessions, dict):
            raise SessionEnvironmentUnavailable("session environment choices are invalid")
        return sessions

    def get(self, session_key: str) -> Optional[str]:
        """The profile id this session runs on, or None for a local session."""
        resolved = self.resolve(session_key)
        return resolved[0] if resolved is not None else None

    def resolve(self, session_key: str) -> Optional[tuple[str, str]]:
        """``(profile id, owner session key)``, or None for a local session.

        The owner key is the session whose workspace this one works in: itself,
        or for a subagent, the parent it inherited the environment from.
        """
        entry = self._read().get(session_key)
        if not isinstance(entry, dict) or entry.get("provider") not in (PROVIDER_ID, "fargate"):
            return None
        profile = entry.get("configuration")
        if not isinstance(profile, str) or not _PROFILE_ID_RE.match(profile):
            return None
        owner = entry.get("parent", session_key)
        if not isinstance(owner, str) or not owner or len(owner) > _MAX_SESSION_KEY:
            # A malformed inheritance row must not fall back to "local".
            raise SessionEnvironmentUnavailable("session environment choices are invalid")
        return profile, owner

    def inherit(self, child_key: str, parent_key: str) -> Optional[str]:
        """Bind ``child_key`` to its parent's environment; the profile, or None.

        Written atomically under the same lock as :meth:`set`, and resolved to the
        parent's OWN workspace owner, so a subagent of a subagent still lands in
        the root session's workspace. Returns None (and writes nothing) when the
        parent runs locally.
        """
        if not child_key or len(child_key) > _MAX_SESSION_KEY or child_key == parent_key:
            raise ValueError("session key is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                sessions = self._read()
                parent = self.resolve(parent_key)
                if parent is None:
                    if sessions.pop(child_key, None) is not None:
                        self._write(sessions)
                    return None
                profile, owner = parent
                sessions[child_key] = {
                    "provider": self._provider(profile),
                    "configuration": profile,
                    "parent": owner,
                }
                self._write(sessions)
                return profile

    def forget_inherited(self, child_key: str) -> bool:
        """Drop ``child_key``'s inherited row; True when one was removed.

        Only a row written by :meth:`inherit` is dropped: a session's own choice
        is never touched here.
        """
        if not child_key or len(child_key) > _MAX_SESSION_KEY:
            raise ValueError("session key is invalid")
        try:
            lock_fd = open(self.lock_path, "a+b")
        except FileNotFoundError:
            # No choices directory: no row was ever written.
            return False
        with lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                sessions = self._read()
                entry = sessions.get(child_key)
                if not isinstance(entry, dict) or "parent" not in entry:
                    return False
                del sessions[child_key]
                self._write(sessions)
                return True

    def _write(self, sessions: dict[str, dict[str, str]]) -> None:
        payload = {"version": _CHOICES_VERSION, "sessions": sessions}
        atomic_write(
            self.path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            fsync=True,
            restrict_to_owner=True,
        )

    def set(self, session_key: str, profile_id: Optional[str]) -> None:
        """Bind a session to a profile, or clear it (None) so it runs locally."""
        if not session_key or len(session_key) > _MAX_SESSION_KEY:
            raise ValueError("session key is invalid")
        if profile_id is not None and not _PROFILE_ID_RE.match(profile_id):
            raise ValueError("profile id is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                sessions = self._read()
                if profile_id is None:
                    sessions.pop(session_key, None)
                else:
                    sessions[session_key] = {
                        "provider": self._provider(profile_id),
                        "configuration": profile_id,
                    }
                self._write(sessions)


@dataclass(frozen=True)
class PreparedRemoteSession:
    launch: RemoteWorkerLaunch
    workdir: str
    workspace: str
    profile_id: str
    #: The profile's display name, for the model-facing context line.
    profile_name: str = ""
    #: True when this prepare created the session's workspace rather than
    #: reattaching its own. A session that already has a conversation is then
    #: told its workspace is empty.
    created: bool = False


async def _login_runner(argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
    """CoderClient runner that uses the CLI's stored login when no token is set.

    CoderClient always exports ``CODER_SESSION_TOKEN``; an empty value would
    override the stored login, so it is dropped instead. HOME is passed through
    because that is where the CLI finds its login.
    """
    if not env.get("CODER_SESSION_TOKEN"):
        env = {k: v for k, v in env.items() if k != "CODER_SESSION_TOKEN"}
    for name in ("HOME", "XDG_CONFIG_HOME", "USER"):
        if name in os.environ:
            env.setdefault(name, os.environ[name])
    # Keep the lifecycle runner's bounded stdout and discarded stderr: CLI
    # diagnostics may contain credentials, even without recognizable labels.
    client = CoderClient(argv[0], env.get("CODER_URL", ""), "", cwd)
    return await client._run(argv, env, cwd)


def resolve_coder_bin() -> str:
    """Absolute path to ``coder``: the gateway may run with a minimal PATH."""
    from kiro_crew.env import augmented_path

    found = shutil.which("coder") or shutil.which("coder", path=augmented_path())
    if found:
        return found
    raise SessionEnvironmentUnavailable(
        "the Coder CLI is not installed on the gateway machine; install it and run coder login"
    )


class CoderSessionEnvironments:
    """Prepare Coder-backed remote sessions from saved profiles."""

    provider_id = PROVIDER_ID

    def __init__(
        self,
        config: Any,
        data_home: Path,
        *,
        coder_bin: Optional[str] = None,
        client_factory: Optional[Callable[[str, str], CoderClient]] = None,
        stop_after_minutes: int = DEFAULT_STOP_AFTER_MINUTES,
    ) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self._coder_bin = coder_bin
        self._client_factory = client_factory
        self.stop_after_minutes = stop_after_minutes
        self.registry = WorkspaceBindingRegistry(self.data_home / "coder" / "session-envs.json")
        self.choices = SessionEnvironmentChoices(self.data_home / "coder" / "session-choices.json")
        self.secrets_dir = self.data_home / "remote-session-secrets"

    @property
    def url(self) -> str:
        url = getattr(self._config, "coder_url", "") or ""
        if not url:
            raise SessionEnvironmentUnavailable(
                "no Coder URL is configured in Session Environments"
            )
        return url

    def profile(self, profile_id: str) -> Any:
        for profile in getattr(self._config, "profiles", []) or []:
            if profile.id == profile_id:
                return profile
        raise SessionEnvironmentUnavailable(
            f"session environment profile {profile_id!r} is not configured"
        )

    def _bin(self) -> str:
        return self._coder_bin or resolve_coder_bin()

    def _client(self) -> CoderClient:
        if self._client_factory is not None:
            return self._client_factory(self._bin(), self.url)
        return CoderClient(self._bin(), self.url, "", self.data_home, runner=_login_runner)

    async def prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        """Reserve, find-or-create, start and verify this session's workspace."""
        from kiro_crew.coder.retention import session_lifecycle

        lifecycle = session_lifecycle(session_key)
        # Invalidate queued closes before waiting for an archive already stopping
        # compute. Its stop/stamp must finish before resume clears the stamp.
        lifecycle.generation += 1
        async with lifecycle.lock:
            return await self._prepare(session_key, profile_id)

    async def _prepare(self, session_key: str, profile_id: str) -> PreparedRemoteSession:
        profile = self.profile(profile_id)
        # Reopening an archived session keeps its workspace: clear the pending
        # deletion before anything else, or refuse while the sweep deletes it.
        # Registry calls run on a worker thread: its file lock is single-shot on
        # the event loop, so a sweeper or browse read holding it would refuse
        # the start instead of waiting.
        from kiro_crew.coder.retention import resume_check

        await asyncio.to_thread(resume_check, self.registry, session_key)
        client = self._client()
        try:
            owner, owner_id = await client.current_user()
        except CoderClientError as exc:
            raise SessionEnvironmentUnavailable(
                "the gateway is not signed in to Coder; run coder login on the gateway machine"
            ) from exc
        binding = await asyncio.to_thread(
            self.registry.allocate,
            session_key,
            template=profile.template,
            preset="",
            prefix=WORKSPACE_PREFIX,
            owner_name=owner,
            deployment_id=self.url,
        )
        created = False

        def verify_workspace(workspace: Any) -> None:
            if workspace.owner not in (owner, owner_id):
                raise SessionEnvironmentUnavailable(
                    "this session's workspace belongs to another Coder user"
                )
            if workspace.template != profile.template:
                raise SessionEnvironmentUnavailable(
                    "this session's workspace uses a different template"
                )
            if binding.workspace_uuid and binding.workspace_uuid != workspace.uuid:
                raise SessionEnvironmentUnavailable(
                    "this session's workspace name now names another workspace"
                )

        try:
            workspace = await client.get_workspace(binding.workspace_name)
            if workspace is not None:
                # Names can be reused: refuse a replacement before starting
                # compute, and verify the returned record again after start.
                verify_workspace(workspace)
            if workspace is None or workspace.status != _RUNNING:
                report_start_stage(session_key, "provisioning")
            if workspace is None:
                if binding.workspace_uuid:
                    # The registry says this session already had a workspace, and
                    # it is gone: never silently recreate under a stale record.
                    raise SessionEnvironmentUnavailable(
                        "this session's workspace was deleted outside Kiro Crew"
                    )
                if binding.template != profile.template:
                    # No workspace was ever created for this row, so the session
                    # may have moved to a profile on another template. Record the
                    # template the create uses: browse and the retention sweep
                    # verify the live workspace against the row's template.
                    binding = await asyncio.to_thread(
                        self.registry.replace, replace(binding, template=profile.template)
                    )
                created = True
                workspace = await client.create_workspace(
                    name=binding.workspace_name,
                    template=profile.template,
                    preset="",
                    stop_after_minutes=self.stop_after_minutes,
                )
            elif workspace.status != _RUNNING:
                if workspace.status not in _STARTABLE:
                    raise SessionEnvironmentUnavailable(
                        f"this session's workspace is {workspace.status or 'unavailable'}; try again shortly"
                    )
                workspace = await client.start_workspace(binding.workspace_name)
        except CoderClientError as exc:
            raise SessionEnvironmentUnavailable(
                f"Coder could not start the workspace: {exc}"
            ) from exc
        verify_workspace(workspace)
        if not binding.workspace_uuid or binding.state != _RUNNING:
            binding = await asyncio.to_thread(
                self.registry.replace,
                replace(binding, workspace_uuid=workspace.uuid, owner_id=owner_id, state=_RUNNING),
            )
        launch = RemoteWorkerLaunch(
            binding.binding_id,
            CoderShell(self._bin(), binding.workspace_name, self.url),
            self.secrets_dir,
        )
        return PreparedRemoteSession(
            launch,
            profile.workdir,
            binding.workspace_name,
            profile.id,
            getattr(profile, "name", "") or profile.id,
            created=created,
        )

    async def prepare_if_chosen(self, session_key: str) -> Optional[PreparedRemoteSession]:
        """Prepare the chosen environment, or None when the session runs locally."""
        if not session_key:
            return None
        profile_id = await asyncio.to_thread(self.choices.get, session_key)
        if profile_id is None:
            return None
        return await self.prepare(session_key, profile_id)


async def prepare_for_session(
    session_key: Optional[str],
    *,
    before_compute: Optional[Callable[[], None]] = None,
) -> Optional[PreparedRemoteSession]:
    """The runtime seam: a prepared remote session, or None to run locally.

    A session runs remotely ONLY when a choice was recorded for its own key. Any
    failure after that raises :class:`SessionEnvironmentUnavailable` rather than
    falling back to local execution, so a session the operator put on remote
    compute never quietly runs on the gateway machine instead.
    """
    if not isinstance(session_key, str) or not session_key:
        return None
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.paths import data_home

    home = await asyncio.to_thread(data_home)
    choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
    resolved = await asyncio.to_thread(choices.resolve, session_key)
    if resolved is None:
        return None
    profile_id, owner_key = resolved
    # Cleared by the caller once the whole start ends (see providers/acp.py).
    report_start_stage(session_key, "checking")
    if before_compute is not None:
        # Refuse an unsupported runtime BEFORE a workspace is created or started.
        before_compute()
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    from kiro_crew.fargate_session import fargate_profile

    if fargate_profile(section, profile_id) is not None:
        from kiro_crew.fargate_session.launch import FargateSessionEnvironments

        # Keyed by the owner like Coder's, so a subagent shares the parent's task.
        return await FargateSessionEnvironments(section, home).prepare(owner_key, profile_id)
    environments = CoderSessionEnvironments(section, home)
    # An inherited (subagent) session works in its owner's workspace: the
    # registry row is keyed by the owner, so this reuses that workspace and never
    # allocates a second one. Each runtime still gets its own guest worker.
    return await environments.prepare(owner_key, profile_id)


async def forget_subagent_environment(child_key: str) -> None:
    """Drop a finished subagent's inherited environment row.

    Best effort: a row left behind only costs bytes, and a later run of the same
    subagent re-inherits before it starts.
    """
    if not child_key:
        return
    from kiro_crew.config.paths import data_home

    try:
        home = await asyncio.to_thread(data_home)
        choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
        await asyncio.to_thread(choices.forget_inherited, child_key)
    except Exception:  # noqa: BLE001 -- runs in a finished run's finally; must not raise
        logger.info("Could not drop the environment row of %s", child_key, exc_info=True)


async def inherit_environment_for_subagent(parent_key: str, child_key: str) -> bool:
    """Bind a subagent's session to its parent's remote environment.

    True when the parent runs remotely (the child now does too), False when it
    runs locally. Raises :class:`SessionEnvironmentUnavailable` when the choice
    cannot be read or written, so a subagent of a remote session is refused
    rather than silently run on the gateway machine.
    """
    if not parent_key or not child_key:
        return False
    from kiro_crew.config.paths import data_home

    home = await asyncio.to_thread(data_home)
    choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
    try:
        profile = await asyncio.to_thread(choices.inherit, child_key, parent_key)
    except (OSError, ValueError) as exc:
        raise SessionEnvironmentUnavailable(
            "could not give this subagent its parent's session environment"
        ) from exc
    return profile is not None

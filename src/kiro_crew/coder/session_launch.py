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
KIRO_CLOUD_PROVIDER_ID = "kiro_cloud"
#: The providers a choice row may record. The profile id still decides the
#: provider at launch (:func:`profile_provider`); the row says where it ran.
CHOICE_PROVIDERS = frozenset({PROVIDER_ID, KIRO_CLOUD_PROVIDER_ID})
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
        resolved = self.resolve_choice(session_key)
        return resolved[:2] if resolved is not None else None

    def resolve_choice(self, session_key: str) -> Optional[tuple[str, str, str]]:
        """``(profile id, owner session key, recorded provider)``, or None for a local session."""
        entry = self._read().get(session_key)
        if not isinstance(entry, dict):
            return None
        provider = entry.get("provider")
        if not isinstance(provider, str) or provider not in CHOICE_PROVIDERS:
            return None
        profile = entry.get("configuration")
        if not isinstance(profile, str) or not _PROFILE_ID_RE.match(profile):
            return None
        owner = entry.get("parent", session_key)
        if not isinstance(owner, str) or not owner or len(owner) > _MAX_SESSION_KEY:
            # A malformed inheritance row must not fall back to "local".
            raise SessionEnvironmentUnavailable("session environment choices are invalid")
        return profile, owner, provider

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
                parent_row = sessions.get(parent_key)
                provider = parent_row.get("provider") if isinstance(parent_row, dict) else None
                sessions[child_key] = {
                    "provider": provider if provider in CHOICE_PROVIDERS else PROVIDER_ID,
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

    def set(self, session_key: str, profile_id: Optional[str], provider: str = PROVIDER_ID) -> None:
        """Bind a session to a profile, or clear it (None) so it runs locally.

        ``provider`` is the provider ``profile_id`` resolves to, recorded on the row.
        """
        if not session_key or len(session_key) > _MAX_SESSION_KEY:
            raise ValueError("session key is invalid")
        if profile_id is not None and not _PROFILE_ID_RE.match(profile_id):
            raise ValueError("profile id is invalid")
        if provider not in CHOICE_PROVIDERS:
            raise ValueError("provider is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                sessions = self._read()
                if profile_id is None:
                    sessions.pop(session_key, None)
                else:
                    sessions[session_key] = {"provider": provider, "configuration": profile_id}
                self._write(sessions)

    def record_cloud_session(self, session_key: str, cloud_session_id: str) -> bool:
        """Record on a Kiro Cloud row the session id Kiro's cloud answered with.

        True when the row now carries it. A row that is gone, or that is not a
        Kiro Cloud row, is left alone (False): the choice changed since the start.
        """
        if not session_key or len(session_key) > _MAX_SESSION_KEY:
            raise ValueError("session key is invalid")
        if not isinstance(cloud_session_id, str) or not _valid_cloud_session_id(cloud_session_id):
            raise ValueError("cloud session id is invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as lock_fd:
            with file_lock(lock_fd.fileno(), exclusive=True, required=True):
                sessions = self._read()
                entry = sessions.get(session_key)
                if not isinstance(entry, dict) or entry.get("provider") != KIRO_CLOUD_PROVIDER_ID:
                    return False
                if entry.get("cloud_session") != cloud_session_id:
                    entry["cloud_session"] = cloud_session_id
                    self._write(sessions)
                return True

    def cloud_session_recorded(self, session_key: str) -> bool:
        """Whether Kiro's cloud holds this session: a cloud session id is on its row.

        A start refused before ``session/new`` records none. A malformed value
        raises :class:`SessionEnvironmentUnavailable`, since it does not prove
        the cloud holds nothing.
        """
        entry = self._read().get(session_key)
        if not isinstance(entry, dict) or "cloud_session" not in entry:
            return False
        value = entry["cloud_session"]
        if not isinstance(value, str) or not _valid_cloud_session_id(value):
            raise SessionEnvironmentUnavailable("session environment choices are invalid")
        return True


def _valid_cloud_session_id(value: str) -> bool:
    return 0 < len(value) <= _MAX_SESSION_KEY and value.isprintable()


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


@dataclass(frozen=True)
class PreparedCloudSession:
    """A session bound to Kiro Cloud Sessions: KAS runs it in a Kiro cloud sandbox.

    Nothing is provisioned here. Unlike every other provider, the agent itself
    runs in Kiro's cloud, so the session carries none of Kiro Crew's tools, its
    agent spec or its PreToolUse gate (see ``AcpRuntime.set_cloud_sandbox``).
    """

    profile_id: str
    profile_name: str
    repositories: tuple[str, ...] = ()


#: The capability an enterprise POLICY names, ``{"enabled": true}``, to allow Kiro
#: Cloud Sessions on a host whose ceiling governs tool execution. Opt-in: a host
#: whose ceiling governs none of :data:`_TOOL_EXECUTION_SCOPES` needs no row.
KIRO_CLOUD_CAPABILITY = "capabilities.kiro_cloud_sessions"
#: Ceiling scopes that bound what a session's tools may do. A Kiro Cloud session's
#: tools run in Kiro's cloud, where Kiro Crew applies none of them.
_TOOL_EXECUTION_SCOPES = (
    "tools",
    "approval_modes",
    "approval_mode",
    "mcp",
    "apps",
    "commands",
    "filesystem.read",
    "filesystem.write",
    "folders.read",
    "folders.write",
    "network.egress",
    "sandbox.min_level",
)


def kiro_cloud_governance_refusal(session_key: str, *, agent: str = "") -> Optional[str]:
    """Why the governance ceiling refuses a Kiro Cloud start for ``session_key``, or None.

    A Kiro Cloud session runs its tools in Kiro's cloud, outside every ceiling
    Kiro Crew enforces, so choosing one would let a local user loosen limits only
    the administrator may loosen. The execution agent's active profile and the
    policy can each deny the cloud capability, even without tool restrictions.
    When either governs a tool-execution scope, only a POLICY opt-in can allow
    cloud execution, and a profile can withdraw it. Fails closed on evaluation
    errors. Blocking (profile resolution and SEL auditing may read/write disk).
    """
    refusal = None
    reason = "no tool-execution scope is governed and the cloud capability is permitted"
    rule = "cloud-launch"
    layer = "default"
    try:
        from kiro_crew.platform.context import current_context
        from kiro_crew.platform.governance import CapabilityGate, resolve
        from kiro_crew.platform.governance_profiles import resolve_active_scope

        ceiling = current_context().governance
        profile = resolve_active_scope(session_key, agent=agent)
        decision = resolve(ceiling, profile, KIRO_CLOUD_CAPABILITY, "")
        governed = sorted(
            {
                scope
                for level in (ceiling, profile)
                if level is not None
                for scope in _TOOL_EXECUTION_SCOPES
                if level.get(scope) is not None
            }
        )
        layer = decision.layer
        if not decision.permitted:
            rule = decision.rule
            refusal = (
                f"{KIRO_CLOUD_CAPABILITY}: {decision.reason}; " "choose another session environment"
            )
        elif governed:
            gate = ceiling.get(KIRO_CLOUD_CAPABILITY) if ceiling is not None else None
            if isinstance(gate, CapabilityGate) and gate.enabled:
                reason = "policy explicitly permits cloud sessions outside tool-execution limits"
            else:
                layer = "policy"
                refusal = (
                    f"this host's governance policy limits {', '.join(governed)}, and a Kiro Cloud "
                    "session's tools run in Kiro's cloud outside those limits; an administrator "
                    f"can allow it with {KIRO_CLOUD_CAPABILITY}, or choose another session environment"
                )
    except Exception:
        logger.warning("Kiro Cloud start: governance could not be read", exc_info=True)
        rule = "evaluation-error"
        layer = "default"
        refusal = (
            "this host's governance policy could not be read, so Kiro Cloud Sessions "
            "are refused; choose another session environment"
        )
    try:
        from kiro_crew.sel import sel

        sel().log_governance_decision(
            session_key=session_key,
            agent=agent or "kirocrew",
            tool_name="session_environment:kiro_cloud",
            scope=KIRO_CLOUD_CAPABILITY,
            outcome="denied" if refusal is not None else "allowed",
            rule=rule,
            layer=layer,
            reason=refusal if refusal is not None else reason,
        )
    except Exception:
        logger.debug("Kiro Cloud start: governance audit failed", exc_info=True)
    return refusal


def kiro_cloud_profile(section: Any, profile_id: str) -> Optional[dict[str, Any]]:
    """The saved Kiro Cloud profile with ``profile_id``, or None."""
    for p in getattr(section, "kiro_cloud_profiles", None) or []:
        if isinstance(p, dict) and p.get("id") == profile_id:
            return p
    return None


def ambiguous_profile_ids(section: Any) -> set[str]:
    """Ids that name both a Coder and a Kiro Cloud profile in the loaded ``section``.

    The config writer refuses that pairing; a hand-edited file can still hold
    one. Such an id names no provider: the picker omits it and a launch refuses it.
    """
    coder = {getattr(p, "id", None) for p in getattr(section, "profiles", None) or []}
    cloud = {
        p.get("id")
        for p in getattr(section, "kiro_cloud_profiles", None) or []
        if isinstance(p, dict)
    }
    return {i for i in coder & cloud if isinstance(i, str)}


def profile_provider(section: Any, profile_id: str) -> Optional[str]:
    """The provider ``profile_id`` names in ``section``, or None when no profile has it.

    The one resolver the launch path, the picker and the choice lock share.
    Raises :class:`SessionEnvironmentUnavailable` for an id that names profiles
    of more than one provider, rather than picking one of them.
    """
    if profile_id in ambiguous_profile_ids(section):
        raise SessionEnvironmentUnavailable(
            f"profile {profile_id!r} is saved for more than one provider; "
            "give each profile its own id in Settings"
        )
    if kiro_cloud_profile(section, profile_id) is not None:
        return KIRO_CLOUD_PROVIDER_ID
    if any(getattr(p, "id", None) == profile_id for p in getattr(section, "profiles", None) or []):
        return PROVIDER_ID
    return None


def chosen_provider(section: Any, profile_id: str, recorded: str) -> Optional[str]:
    """The provider a session bound to ``profile_id`` runs on, checked against its row.

    ``recorded`` is the provider the choice row was written with. An id that now
    names a profile of ANOTHER provider (the original deleted, the id reused) is
    refused rather than moving the session there: its files or its history live
    with the provider it started on. None when no profile has the id.
    """
    provider = profile_provider(section, profile_id)
    if provider is not None and provider != recorded:
        raise SessionEnvironmentUnavailable(
            f"profile {profile_id!r} now names a {provider} profile, but this session "
            f"runs on {recorded}; choose its environment again or start a new chat"
        )
    return provider


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
        # Refuses an id that a profile of another provider also uses.
        profile_provider(self._config, profile_id)
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
    agent: str = "",
    before_compute: Optional[Callable[[], None]] = None,
) -> "Optional[PreparedRemoteSession | PreparedCloudSession]":
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
    resolved = await asyncio.to_thread(choices.resolve_choice, session_key)
    if resolved is None:
        return None
    profile_id, owner_key, recorded_provider = resolved
    # Cleared by the caller once the whole start ends (see providers/acp.py).
    report_start_stage(session_key, "checking")
    if before_compute is not None:
        # Refuse an unsupported runtime BEFORE a workspace is created or started.
        before_compute()
    config = live.snapshot()
    if config is None:
        config = await asyncio.to_thread(KiroCrewConfig.load)
    section = config.session_environments
    # The id names the provider, which must be the one the row recorded; an id
    # saved for two providers is refused.
    if chosen_provider(section, profile_id, recorded_provider) == KIRO_CLOUD_PROVIDER_ID:
        refusal = await asyncio.to_thread(kiro_cloud_governance_refusal, session_key, agent=agent)
        if refusal is not None:
            raise SessionEnvironmentUnavailable(refusal)
        cloud = kiro_cloud_profile(section, profile_id) or {}
        # Nothing to provision: KAS creates the sandbox at session/new.
        return PreparedCloudSession(
            profile_id=profile_id,
            profile_name=str(cloud.get("name") or profile_id),
            repositories=tuple(str(r) for r in cloud.get("repositories") or ()),
        )
    environments = CoderSessionEnvironments(section, home)
    # An inherited (subagent) session works in its owner's workspace: the
    # registry row is keyed by the owner, so this reuses that workspace and never
    # allocates a second one. Each runtime still gets its own guest worker.
    return await environments.prepare(owner_key, profile_id)


async def record_cloud_session(session_key: str, cloud_session_id: str) -> None:
    """Record that Kiro's cloud now holds ``session_key``, which fixes its environment.

    Called once ``session/new`` or ``session/load`` answered on a Kiro Cloud
    runtime. Raises :class:`SessionEnvironmentUnavailable` when the record
    cannot be written, so no turn runs in the cloud on a chat that could still
    be moved to this machine.
    """
    from kiro_crew.config.paths import data_home

    home = await asyncio.to_thread(data_home)
    choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
    try:
        await asyncio.to_thread(choices.record_cloud_session, session_key, cloud_session_id)
    except (OSError, ValueError) as exc:
        raise SessionEnvironmentUnavailable(
            "could not record this session's Kiro Cloud session"
        ) from exc


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

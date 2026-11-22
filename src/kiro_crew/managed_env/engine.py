"""The managed-environment launch lane: the core's ``LaunchEngine`` for Remote Crew.

``preflight`` checks every setting, the sign-in and the service. ``provision`` creates
an environment named ``<crew>-<launch tag>``, waits for it to run, installs Crew and
machine mode, and writes the boot hook; any failure after the create deletes the
environment by its ledger id. ``begin_signin`` reads the box's machine sign-in.
``register`` adds the Remote Crew row, runs the hook and waits for the gateway.
``teardown`` deletes by ledger id only, reads the List once for its answer, and removes
nothing.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.managed_env import PROVISIONER_ID, install, machine
from kiro_crew.managed_env import remote as sbx_remote
from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.managed_env.client import (
    GuardRefused,
    SandboxClient,
    SandboxError,
    SandboxStateError,
)
from kiro_crew.managed_env.guard import SandboxGuard
from kiro_crew.managed_env.settings import LaneSettings, SettingsMissing
from kiro_crew.managed_env.signin_remote import SandboxSigninHandle

logger = logging.getLogger(__name__)

_CREW_RE = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")
INSTALL_TIMEOUT_S = 900.0
STEP_TIMEOUT_S = 120.0

TEXT = {
    "bad_crew_name": "A crew name is 2 to 32 lowercase letters, digits or single hyphens.",
    # An environment with no Remote Crew row has no Delete in the dashboard, so the
    # route stays named for that case.
    "quota": "You have the most managed environments allowed. Delete one "
    "(Delete environment on its Remote Crew row, or "
    "POST /api/managed-environments/<id>/delete for one with no row), then launch again.",
    "install_failed": "Crew could not be installed on {host} ({why}).",
    "not_running": "{host} did not start ({why}).",
    "hook_failed": "Crew's gateway did not start on {host} ({why}). Connect again, or delete it "
    "(Delete environment on its Remote Crew row).",
    "not_machine": "Crew on {host} is not on machine mode ({why}). Check the environment, then "
    "Connect again.",
    "no_health": "{host} is in Remote Crew, but its gateway did not answer. Connect again, or "
    "delete it (Delete environment on its Remote Crew row).",
    "no_row": "{host} is running but could not be added to Remote Crew ({why}). Delete it "
    "(POST /api/managed-environments/<id>/delete).",
    "no_machine_mode": "This Crew release has no machine mode, which a managed environment's "
    "Crew needs. Update Crew, then launch again.",
    "ssh_config_missing": "Add the managed-environment ssh settings first "
    "(Allow this computer to connect, on the launch form), then launch again.",
}


class SandboxEngineError(RuntimeError):
    """A launch step that stopped. ``code`` is a stable word; the message is plain text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_crew_name(name: str) -> str:
    if not isinstance(name, str) or not _CREW_RE.fullmatch(name) or "--" in name:
        raise SandboxEngineError("bad_crew_name", TEXT["bad_crew_name"])
    return name


def _state_dir() -> Path:
    from kiro_crew.config.paths import config_dir

    return config_dir() / "managed-env"


_shared: Optional[SandboxClient] = None
_shared_lock = threading.Lock()


def shared_client() -> SandboxClient:
    """One client (and one guard) per gateway, so the backoff holds across launches."""
    global _shared
    with _shared_lock:
        if _shared is None:
            from kiro_crew.managed_env.core_signin import vault_credential

            _shared = SandboxClient(vault_credential, SandboxGuard(_state_dir()))
        return _shared


def _engine_error(exc: Exception) -> SandboxEngineError:
    if isinstance(exc, SandboxEngineError):
        return exc
    code = getattr(exc, "kind", None) or getattr(exc, "reason", None) or "service_error"
    return SandboxEngineError(str(code), str(exc))


class SandboxLaunchEngine:
    def __init__(
        self,
        *,
        client: Optional[SandboxClient] = None,
        registry_factory: Optional[Callable[[], Any]] = None,
        run: Optional[Callable[..., sbx_remote.RemoteResult]] = None,
        settings: Optional[Callable[[], LaneSettings]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        machine_shipped: Optional[Callable[[], bool]] = None,
        ssh_block: Optional[Callable[[], bool]] = None,
    ) -> None:
        from kiro_crew.managed_env import ssh_config

        self._machine_shipped = machine_shipped or machine.shipped
        self._ssh_block = ssh_block or ssh_config.block_current
        self._client = client
        self._registry_factory = registry_factory
        self._run = run or sbx_remote.run_script
        self._settings = settings or (lambda: lane_settings.read_settings())
        self._clock = clock
        self._sleep = sleep
        #: Launch tags whose create was refused outright, so a teardown can say "nothing made".
        self._never_created: set[str] = set()

    @property
    def client(self) -> SandboxClient:
        return self._client if self._client is not None else shared_client()

    def _registry(self) -> Any:
        if self._registry_factory is not None:
            return self._registry_factory()
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry()

    def _checked(self) -> tuple[LaneSettings, lane_settings.Cell]:
        """Every setting a launch needs, or one refusal naming every missing key."""
        s = self._settings()
        missing: list[str] = []
        cell = None
        for step in (s.service_endpoint, s.ssh_region_or_raise):
            try:
                step()
            except SettingsMissing as exc:
                missing += [k.split(".", 1)[1] for k in exc.keys]
        try:
            cell = s.cell()
        except SettingsMissing as exc:
            missing += [k.split(".", 1)[1] for k in exc.keys]
        if missing or cell is None:
            raise SandboxEngineError("not_configured", str(SettingsMissing(missing)))
        return s, cell

    # -- LaunchEngine ---------------------------------------------------------------

    def preflight(self, profile: str, region: str = "") -> None:
        validate_crew_name(profile)
        self._checked()
        if not self._machine_shipped():
            raise SandboxEngineError("no_machine_mode", TEXT["no_machine_mode"])
        if not self._ssh_block():
            raise SandboxEngineError("ssh_config_missing", TEXT["ssh_config_missing"])
        try:
            self.client.list_sandboxes()
        except (SandboxError, GuardRefused) as exc:
            raise _engine_error(exc) from None

    def provision(
        self, *, tag: str, size_key: str = "", profile: str, region: str = "", **_: Any
    ) -> str:
        crew = validate_crew_name(profile)
        try:
            cell = self._checked()[1]
        except SandboxEngineError:
            self._never_created.add(tag)
            raise
        try:
            created = self.client.create_sandbox(f"{crew}-{tag}")
        except (SandboxError, GuardRefused) as exc:
            # Refused before anything was sent, or answered with a non-200: nothing was made.
            # A 200 with no id (create_no_id) may have made one, so it is not "never created".
            refused = isinstance(exc, GuardRefused) and exc.reason != "create_no_id"
            unsent = (
                isinstance(exc, SandboxError)
                and exc.status is None
                and exc.kind in ("no_token", "not_configured")
            )
            if (
                refused
                or unsent
                or (isinstance(exc, SandboxError) and exc.status and exc.status != 200)
            ):
                self._never_created.add(tag)
            if getattr(exc, "kind", "") == "quota":
                raise SandboxEngineError("quota", TEXT["quota"]) from None
            raise _engine_error(exc) from None
        sid = str(created["execEnvironmentId"])
        host = sbx_remote.ssh_host_for(sid)
        try:
            self.client.wait_running(sid)
        except (SandboxError, SandboxStateError, GuardRefused) as exc:
            self._delete_quietly(sid)
            why = getattr(exc, "kind", "") or type(exc).__name__
            raise SandboxEngineError(
                "not_running", TEXT["not_running"].format(host=host, why=why)
            ) from None
        except BaseException:
            self._delete_quietly(sid)
            raise
        try:
            self._install(host, cell)
        except BaseException as exc:
            self._delete_quietly(sid)
            if isinstance(exc, Exception):
                why = str(exc) if isinstance(exc, SandboxEngineError) else type(exc).__name__
                raise SandboxEngineError(
                    "install_failed", TEXT["install_failed"].format(host=host, why=why)
                ) from None
            raise
        return host

    def _install(self, host: str, cell: lane_settings.Cell) -> None:
        steps = (
            (
                "install",
                install.install_script(),
                INSTALL_TIMEOUT_S,
                (install.INSTALL_DONE, install.INSTALL_ALREADY),
            ),
            ("machine-config", machine.config_script(cell), STEP_TIMEOUT_S, (machine.CONFIG_DONE,)),
            (
                "hook-write",
                install.hook_write_script(cell.env_drop),
                STEP_TIMEOUT_S,
                (install.HOOK_WRITTEN,),
            ),
        )
        for name, script, timeout, done in steps:
            res = self._run(host, script, timeout=timeout, retry=True)
            if not any(line.strip() in done for line in res.stdout.splitlines()):
                raise SandboxEngineError(name, f"{name} exit {res.returncode}")

    def _delete_quietly(self, sid: str) -> bool:
        """One cleanup Delete by ledger id; a refused or failed one is logged and False."""
        try:
            self.client.delete_sandbox(sid, cleanup=True)
            return True
        except Exception as exc:  # noqa: BLE001 - the launch's own failure is the one shown
            logger.warning(
                "could not delete environment %s after a failed launch (%s)",
                sid,
                type(exc).__name__,
            )
            return False

    def login_target_refusal(self, target: Any) -> str:
        """Why a non-default sign-in target cannot be honoured, else ``""``: the box's
        Crew runs on its machine sign-in, never on a person's."""
        from kiro_crew.managed_env import signin_remote

        return signin_remote.MACHINE_ONLY if signin_remote.asks_another_identity(target) else ""

    def begin_signin(
        self, *, instance_id: str, profile: str, region: str, login_target: Any = None
    ) -> SandboxSigninHandle:
        return SandboxSigninHandle(instance_id, self._run, login_target)

    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        from kiro_crew.cloud.launch_job import RegistrationUnavailable

        crew = validate_crew_name(profile)
        try:
            self._write_row(crew, instance_id)
        except Exception as exc:
            raise RegistrationUnavailable(
                TEXT["no_row"].format(host=instance_id, why=type(exc).__name__)
            ) from exc
        res = self._run(instance_id, install.HOOK_RUN, timeout=STEP_TIMEOUT_S, retry=True)
        state = install.hook_state(res.returncode, res.stdout)
        if state in ("not_machine", "needs_signin"):
            raise RegistrationUnavailable(TEXT["not_machine"].format(host=instance_id, why=state))
        if state not in ("started", "alive"):
            raise RegistrationUnavailable(TEXT["hook_failed"].format(host=instance_id, why=state))
        if not sbx_remote.probe_health(
            self._run, instance_id, clock=self._clock, sleep=self._sleep
        ):
            raise RegistrationUnavailable(TEXT["no_health"].format(host=instance_id))

    def _write_row(self, crew: str, host: str) -> None:
        if not sbx_remote.sandbox_id_from_host(host):
            raise SandboxEngineError("bad_instance", "not a managed-environment ssh alias")
        reg = self._registry()
        fields = dict(
            connection_method="ssh",
            ssh_host=host,
            remote_port=sbx_remote.GATEWAY_PORT,
            remote_bin=sbx_remote.WRAPPER,
            provisioner_id=PROVISIONER_ID,
        )
        for row in reg.list():
            if row.ssh_host == host:
                reg.update(row.id, **fields)
                return
        reg.add(name=crew, **fields)

    def _ledger_rows_for(self, tag: str) -> list[dict]:
        return [
            r
            for r in self.client._guard.ledger()
            if str(r.get("displayName", "")).endswith(f"-{tag}") or r.get("displayName") == tag
        ]

    def resource_exists(self, *, tag: str, profile: str = "", region: str = "") -> bool:
        """Whether the launch *tag* ever created an environment: a refused create (a
        missing setting, the account's cap) made none, so a rollback must not report one
        removed. Unsure (an unreadable ledger) reads as created."""
        if tag not in self._never_created:
            return True
        try:
            return bool(self._ledger_rows_for(tag))
        except GuardRefused:
            return True

    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        """Delete the launch's environments by ledger id; True only when one List read
        then shows none of them, or shows them ``DELETED``.

        Reached only from the launch job's rollback and cancel. Every ledger row of the
        tag without ``deletedAt`` gets one Delete; a refused or failed Delete, a listed
        status other than ``DELETED``, or an unreadable List leaves it unconfirmed. It
        removes nothing, so its answer only shapes the rollback message."""
        try:
            rows = self._ledger_rows_for(tag)
        except GuardRefused:
            logger.warning(
                "the managed-environment ledger is unreadable; nothing deleted for %s", tag
            )
            return False
        if not rows:
            # No id, no call: a name is never a key.
            return tag in self._never_created
        ids = {str(r["execEnvironmentId"]) for r in rows}
        confirmed = True
        for r in rows:
            if "deletedAt" not in r:
                confirmed = self._delete_quietly(str(r["execEnvironmentId"])) and confirmed
        try:
            listed = self.client.list_sandboxes()
        except (SandboxError, GuardRefused) as exc:
            logger.warning(
                "could not confirm environments for %s were deleted (%s)", tag, type(exc).__name__
            )
            return False
        return confirmed and not any(
            e.get("execEnvironmentId") in ids and e.get("status") != "DELETED" for e in listed
        )

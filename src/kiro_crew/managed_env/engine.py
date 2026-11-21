"""The managed-environment ``LaunchEngine``: preflight, create, register, tear down.

Driven by the core's ``cloud/launch_job.run_launch``, which owns cancel, rollback and
orphan reaping. The three generic launch wires carry this lane's meaning:

=============  ==============================================================
``profile``    the **crew name**: the Remote Crew row's name
``size_key``   the Create form's settings (``idle=<minutes>``, :func:`create_options`)
``region``     ignored; the service has one endpoint
``tag``        the launch tag ``kc-<6 hex>``; the environment's ``displayName`` is
               ``<crew>-<tag>`` (:func:`crew_display_name`)
=============  ==============================================================

What each method guarantees:

* ``preflight`` checks the lane's settings (:meth:`_machine_config`: the service endpoint
  and profile, and every value machine mode writes on the box), then a bearer and a
  ``ListSandboxes``, before anything is created. A missing value is refused by its key.
  The service's own cap on live environments governs; the client adds none.
* ``provision`` makes the one ``CreateSandbox`` call (the client writes the ledger the
  moment the answer arrives), then polls ``GetSandbox`` to RUNNING, then installs Crew the
  way a user would (:func:`.install.install_path_a`), writes machine mode
  (:func:`.install.machine_config`) and the boot hook (:func:`.install.write_hook`), all
  over the ssh alias. Any failure after the create deletes the environment by its ledger
  id. A Create refused at the account's cap raises ``sandbox_quota`` (nothing was created,
  so nothing is deleted, and the client is not stopped). An install that leaves no Crew on
  the bundle route raises ``install_failed``; a failed download, or one refused with HTTP
  403, names the network step. It returns ``crew-sbx-<id>``, the ssh alias and the
  registry row's ``ssh_host``.
* ``begin_signin`` asks the box's Crew ``kas whoami`` over ssh (:mod:`.signin_remote`):
  machine mode is the box's one sign-in, so no device sign-in is ever started. It never
  raises: a failure is ``handle.error``.
* ``register`` adds (or repoints) the crew's row, with ``remote_bin`` set to the launcher
  the install step writes on ``/projects`` (:data:`REMOTE_BIN`), then runs the boot hook to
  start the gateway (:func:`.install.run_hook`), then probes the gateway's ``/api/health``
  every 2 s for up to 120 s (:func:`.remote.probe_health`, on the environment's own
  loopback by ssh exec). A failed row write, a hook that did not start the gateway, or a
  probe that never answers, raises :class:`RegistrationUnavailable` with the way out: the
  environment is up and billing, and its row stays.
* ``teardown`` deletes **by ledger id only**, found from the tag. No ledger entry means
  no call. It is True only after Get answers 404 and List does not show the id.

No bearer, ssh session or device code is ever put in a message or a log.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from kiro_crew.managed_env import PROVISIONER_ID, call_log, core_signin, install
from kiro_crew.managed_env import remote as sbx_remote
from kiro_crew.managed_env import settings as lane_settings
from kiro_crew.managed_env.client import (
    BffClient,
    SandboxError,
    SandboxStateError,
)
from kiro_crew.managed_env.guard import (
    POLICY_TEXT,
    SERVICE_TEXT,
    ClientStopped,
    GuardRefused,
    SandboxGuard,
    crew_tag_of,
    is_policy_refusal,
    is_quota_refusal,
    stopped_line,
)
from kiro_crew.managed_env.install import WRAPPER
from kiro_crew.managed_env.netcfg_step import MODE_TO as NETWORK_STEP_MODE
from kiro_crew.managed_env.netcfg_step import SETTINGS_PATH as NETWORK_SETTINGS_PATH
from kiro_crew.managed_env.settings import (
    LaneSettings,
    SettingsMissing,
)
from kiro_crew.managed_env.signin_remote import SandboxSigninHandle

logger = logging.getLogger(__name__)

#: The ssh alias prefix of a managed environment: ``crew-sbx-<execEnvironmentId>``.
SSH_HOST_PREFIX = "crew-sbx-"
REMOTE_PORT = sbx_remote.GATEWAY_PORT
#: The Remote Crew row's ``remote_bin``: the install step's launcher on /projects, which carries
#: ``KIROCREW_HOME``. The core's token mint runs an explicit ``remote_bin`` verbatim and skips its
#: run-marker lookup, so the mint reaches the install on /projects.
REMOTE_BIN = WRAPPER
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CREW_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$")

#: The installer's download failed: the environment's network did not let it through.
WHY_DOWNLOAD_FAILED = install.WHY_FETCH_FAILED
#: A curl error line that carries HTTP 403, or pip's proxy error for one (``Tunnel connection
#: failed: 403``): the download host or the egress proxy refused it.
_REFUSED_403 = re.compile(r"curl: \(\d+\)[^|]*\b403\b|Tunnel connection failed: 403\b")
#: The step detail and ``job.error`` keep this many characters of a message.
CARD_TEXT_LIMIT = 400


class SandboxEngineError(RuntimeError):
    def __init__(self, code: str, message: str = "", *, plain: bool = False) -> None:
        # plain: a line for people, shown without its code
        coded = f"{code}: {message}" if message else code
        super().__init__(message if plain and message else coded)
        self.code = code
        self.message = message


class InstallSshRunner(install.SshRunner):
    """The install's runner: :class:`.install.SshRunner`'s
    ``ssh -o BatchMode=yes <alias> <command>`` with its 900 s bound, spawned the way the lane's
    other ssh execs are (the core's ``ssh_spawn_argv_env``). A dashboard started by a GUI has a
    short ``PATH``, where the alias's ``ProxyCommand`` would not find ``session-manager-plugin``."""

    def run(self, host: str, command: str, stdin: bytes | None = None) -> install.RunResult:
        from kiro_crew.instances.token_mint import ssh_spawn_argv_env

        argv, env = ssh_spawn_argv_env(["ssh", "-o", "BatchMode=yes", host, command])
        p = subprocess.run(
            argv,
            env=env,
            input=stdin if stdin is not None else b"",
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        return install.RunResult(p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"))


def install_failed_text(host: str, why: str, detail: str = "") -> str:
    """``<reason>: <detail>`` for ``install_failed``. The way out comes first, because the
    launch card keeps only :data:`CARD_TEXT_LIMIT` characters."""
    text = f"{why}: Crew could not be installed on {host}."
    refused = bool(_REFUSED_403.search(detail))
    if refused or why == WHY_DOWNLOAD_FAILED:
        what = "was refused (HTTP 403)" if refused else "failed"
        text += (
            f" Its download {what}: the environment's network setting may not allow the "
            f"install's hosts. Agree to the network step ({NETWORK_STEP_MODE}) when you launch, "
            f"or set it in {NETWORK_SETTINGS_PATH}, then launch again."
        )
    return f"{text} {detail}".rstrip()


def gateway_needs_signin_text(host: str) -> str:
    """The hook exited 3: the box's Crew reports no machine sign-in, so no gateway was started."""
    return (
        f"{host} has no machine sign-in for Crew yet, so its gateway was not started. Check "
        "the environment's machine token, then start the gateway: Stop and Resume it under "
        f"Your crews, or run `ssh {host} sh {install.HOOK}`. Then Connect."
    )


def gateway_not_kas_bundle_text(host: str) -> str:
    """The hook exited 4: Crew's engine on the box is not on the bundle route."""
    return (
        f"{host} is running and Crew is installed, but Crew's `kas route` did not answer "
        f"{install.ROUTE_KAS_BUNDLE}, so it can't run on the environment's machine token. "
        "The gateway cannot start. Delete it under Your crews and launch again."
    )


def gateway_hook_failed_text(host: str, why: str) -> str:
    return (
        f"{host} is running and in Remote Crew, but its gateway hook did not start the gateway "
        f"({why}). Run `ssh {host} sh {install.HOOK}` to see why, then Connect, or delete it "
        "under Your crews."
    )


def sandbox_quota_text(listed: int | None, other: int | None) -> str:
    """CreateSandbox was refused at the service's cap. Plain words, said at the Create step.

    The count is the List's, read just before the Create (``SandboxError.listed``); the
    service's own number is not written in, so a moved cap still reads true. The refusal is
    definite (nothing was created), so the user can launch again once a place is free.
    ``other`` is kept for the caller's shape. It fits :data:`CARD_TEXT_LIMIT`."""
    if listed:
        have = f"You have {listed} sandboxes, the most allowed."
    else:
        have = "You have the most sandboxes allowed."
    return f"{have} Delete one under Your crews to create another."


def not_configured_text(exc: SettingsMissing) -> str:
    return f"{exc} Nothing was created."


def ssh_host_for(exec_environment_id: str) -> str:
    return SSH_HOST_PREFIX + exec_environment_id


def sandbox_id_from_host(ssh_host: str) -> str:
    """The environment id an ``ssh_host`` names, or "" when it is not a lane alias."""
    if not isinstance(ssh_host, str) or not ssh_host.startswith(SSH_HOST_PREFIX):
        return ""
    sid = ssh_host[len(SSH_HOST_PREFIX) :]
    return sid if _ID_RE.fullmatch(sid) else ""


def validate_crew_name(name: str) -> str:
    if not _CREW_RE.fullmatch(name or "") or "--" in name:
        raise SandboxEngineError(
            "bad_crew_name",
            "Crew name must be 2-32 lowercase letters, digits or single hyphens, "
            "start with a letter and end with a letter or digit.",
        )
    return name


def default_token_provider() -> str:
    """The lane user's bearer from Crew's own sign-in (the vault), or a refusal.

    Never kiro-cli's, and never another identity's (``core_signin.vault_token``). The
    bearer goes to the client and nowhere else; a refusal holds none."""
    try:
        return core_signin.vault_token()
    except core_signin.NotLaneSignIn as exc:
        raise SandboxEngineError("not_signed_in", str(exc)) from None


def _state_dir() -> Path:
    from kiro_crew.config.paths import config_dir

    return Path(config_dir()) / "managed-env"


_shared_lock = threading.Lock()
_shared: dict[str, Any] = {}


def shared_client(token_provider: Callable[[], str] = default_token_provider) -> BffClient:
    """One client (and one guard) per process: the stop rules and the 1 s call gap are
    process-wide, and the engine and the routes must not each have their own."""
    with _shared_lock:
        client = _shared.get("client")
        if client is None:
            state = _state_dir()
            client = BffClient(
                token_provider, SandboxGuard(state), call_log=call_log.from_env(state)
            )
            client.accept_fingerprint = crews_from_list_fingerprint
            _shared["client"] = client
        return client


#: Start Crew's lines, one per hook exit. The hook's ``kas whoami`` checks only the machine
#: token file, so exit 3 means that file is missing or empty.
START_CREW_TEXT = {
    "needs_token": "Crew on this sandbox needs a fresh token. Press Connect, then Start Crew.",
    "files": "Crew's files aren't complete on this sandbox.",
    "launcher": "Crew's gateway didn't start on this sandbox. Try Start Crew again.",
    "other": "Crew's gateway didn't start on this sandbox (exit {rc}).",
}


def start_crew_line(rc: int) -> str:
    if rc == install.NEEDS_SIGNIN_EXIT:
        return START_CREW_TEXT["needs_token"]
    if rc == install.NOT_KAS_BUNDLE_EXIT:
        return START_CREW_TEXT["files"]
    if rc == install.LAUNCHER_FAILED_EXIT:
        return START_CREW_TEXT["launcher"]
    return START_CREW_TEXT["other"].format(rc=rc)


#: Set up Crew's lines (and the read after a Resume), one per result of the one read.
SET_UP_CREW_TEXT = {
    "found": "Crew is on this sandbox. It's under Your crews now.",
    "not_found": (
        "Crew can't be set up on a sandbox it didn't create yet. To use Crew, create a new"
        " sandbox under Set up a new one."
    ),
    "failed": "Crew couldn't check this sandbox. Try Set up Crew again.",
}


def adopted_crew_name(display_name: str, sandbox_id: str) -> str:
    """The Remote Crew row's name for an adopted box: its displayName when that is a valid crew
    name, else the displayName folded to one, else ``sbx-`` and the id's first letters/digits."""
    name = display_name or ""
    if _CREW_RE.fullmatch(name) and "--" not in name:
        return name
    folded = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")[:32]
    folded = folded.rstrip("-")
    if _CREW_RE.fullmatch(folded):
        return folded
    tail = re.sub(r"[^a-z0-9]", "", sandbox_id.lower())[:8] or "box"
    return f"sbx-{tail}"


def crew_display_name(profile: str, tag: str) -> str:
    """The displayName a Crew launch gives its environment: the crew name the user typed, one
    hyphen, then the launch tag. Every dashboard of this user then names the row the same
    (``guard.crew_name_of``), and the tag stays the last part (``guard.crew_tag_of``), so the
    guard's marker and the rollback by tag hold."""
    return f"{profile}-{tag}"


#: Rename's lines on the row. There is no dashboard-only rename, so ``off`` (the service's
#: rename is off) and ``no_shared`` (a box with no tag) fail here too.
RENAME_TEXT = {
    "off": "Rename failed: renaming isn't available yet.",
    "no_shared": "Rename failed: this crew has no shared name to rename.",
    "try_later": "Crew couldn't rename this sandbox right now. Try again later.",
    "taken": "Another crew here already has that name.",
    "no_row": "This sandbox has no crew row here to rename.",
}


def crews_from_list_applies() -> bool:
    """Whether the user's own List decides which environments are crews: the lane is on."""
    return lane_settings.lane_enabled()


def crews_from_list_fingerprint() -> str:
    """The vault's sign-in fingerprint while the lane is on, else ``""``. Blocking file IO
    (it may wait on the vault's sign-out lock): the List's thread, and a client call's own
    thread for an accepted id; never a route check."""
    if not crews_from_list_applies():
        return ""
    try:
        from kiro_crew.auth.bridge import vault_identity_fingerprint

        return vault_identity_fingerprint()
    except Exception:
        logger.warning(
            "could not read the sign-in fingerprint; the List decides nothing", exc_info=True
        )
        return ""


def reset_shared_client() -> None:
    """For tests."""
    with _shared_lock:
        _shared.clear()


class SandboxLaunchEngine:
    def __init__(
        self,
        *,
        client: Optional[BffClient] = None,
        registry_factory: Optional[Callable[[], Any]] = None,
        ssh: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        install_runner: Optional[install.Runner] = None,
        settings: Optional[Callable[[], LaneSettings]] = None,
    ) -> None:
        self._client = client
        self._ssh = ssh or sbx_remote.run_script
        self._clock = clock
        self._sleep = sleep
        self._registry_factory = registry_factory
        #: The install and hook commands' runner (``install.Runner``); tests inject a fake.
        self._install_runner = install_runner
        #: The lane's settings, read at each launch step; tests inject them.
        self._settings = settings or (lambda: lane_settings.read_settings())
        #: Tags whose CreateSandbox was refused outright (nothing was created), so a
        #: teardown with no ledger entry can say so truthfully.
        self._never_created: set[str] = set()

    @property
    def client(self) -> BffClient:
        return self._client if self._client is not None else shared_client()

    @property
    def guard(self) -> SandboxGuard:
        return self.client._guard  # the client owns it; the engine reads the ledger through it

    @property
    def install_runner(self) -> install.Runner:
        if self._install_runner is None:
            self._install_runner = InstallSshRunner()
        return self._install_runner

    def _registry(self) -> Any:
        if self._registry_factory is not None:
            return self._registry_factory()
        from kiro_crew.instances.registry import InstancesRegistry

        return InstancesRegistry()

    def _machine_config(self) -> tuple[install.MachineConfig, LaneSettings]:
        """What machine mode writes on the box, from the lane's settings, or ``not_configured``
        naming every missing key. Read before anything is listed or created."""
        s = self._settings()
        try:
            s.require_service()
            cell = s.machine_cell()
            install.hook_unset_env(_hook_unset(s))  # a bad name refuses here, not on the box
        except SettingsMissing as exc:
            raise SandboxEngineError(
                "not_configured", not_configured_text(exc), plain=True
            ) from None
        except install.InstallError as exc:
            raise SandboxEngineError("not_configured", str(exc), plain=True) from None
        mc = install.MachineConfig(
            cell=cell,
            token_path=s.box_token_path,
            token_env=s.box_token_env,
            env_drop=s.box_env_drop,
            registry_mode=s.mcp_registry_mode,
        )
        return mc, s

    # ── LaunchEngine: preflight ──────────────────────────────────────────────
    def preflight(self, profile: str, region: str) -> None:
        validate_crew_name(profile)
        self._machine_config()  # every refusal of the settings: here, before anything is listed
        try:
            self.client.list_sandboxes()
        except (SandboxError, GuardRefused) as exc:
            raise _engine_error(exc) from None

    # ── LaunchEngine: provision ──────────────────────────────────────────────
    def provision(
        self, *, tag: str, size_key: str = "", profile: str, region: str = "", **_: Any
    ) -> str:
        validate_crew_name(profile)
        try:
            # the Create form's settings ride in the provisioner's own size_key
            idle = create_options(size_key)
            mc, s = self._machine_config()  # read again: refused before the create
        except SandboxEngineError:
            self._never_created.add(tag)
            raise
        display = crew_display_name(profile, tag)
        try:
            created = self.client.create_sandbox(display, idle_timeout_minutes=idle)
        except GuardRefused as exc:
            self._never_created.add(tag)
            raise _engine_error(exc) from None
        except SandboxError as exc:
            if exc.status and 400 <= exc.status < 500:
                self._never_created.add(tag)
            if exc.kind == "not_configured":
                self._never_created.add(tag)
            if idle is not None and exc.status == 400 and IDLE_SWITCH_MARK in (exc.message or ""):
                # idle stop is off for the account. The form turns the field off (the List's
                # idle_off) and says so; Launch again sends no timeout. No retry here.
                note_idle_switch_off()
                raise SandboxEngineError("idle_off", CREATE_IDLE_OFF_TEXT, plain=True) from None
            if is_quota_refusal(exc.op, exc.status, exc.kind):
                # The cap is taken before any create: nothing to delete, and the client runs on.
                raise SandboxEngineError(
                    "sandbox_quota", sandbox_quota_text(exc.listed, exc.listed_other), plain=True
                ) from None
            raise _engine_error(exc) from None
        sid = str(created["execEnvironmentId"])
        try:
            self.client.wait_running(sid, start_shown=str(created.get("status")) != "STOPPED")
        except BaseException as exc:
            # The ledger already holds the id. An environment left behind would bill with
            # no row, so remove it now; the core's rollback finds it by tag as well.
            self._delete_quietly(sid)
            if isinstance(exc, (SandboxError, SandboxStateError, GuardRefused)):
                raise _engine_error(exc) from None
            raise
        host = ssh_host_for(sid)
        try:
            self._install(host, mc, s)
        except BaseException as exc:
            # An environment without Crew is of no use and bills: delete it by its ledger id,
            # as for a failed RUNNING wait.
            self._delete_quietly(sid)
            if isinstance(exc, SandboxEngineError):
                raise
            if isinstance(exc, Exception):
                raise SandboxEngineError(
                    "install_failed", install_failed_text(host, *_install_exception_why(host, exc))
                ) from None
            raise
        return host

    def _install(
        self, host: str, mc: install.MachineConfig, s: LaneSettings
    ) -> install.InstallResult:
        """Crew on ``/projects``, machine mode, then the boot hook: the core's "create +
        install" step. The hook is written here and run by ``register``."""
        runner = self.install_runner
        result = install.install_path_a(runner, host, openssl=s.box_openssl, sleep=self._sleep)
        logger.info("Crew install on %s: %s", host, result.line())
        if result.status not in ("installed", "already") or result.why:
            why = result.why or _reason(result.detail) or result.status
            raise SandboxEngineError(
                "install_failed", install_failed_text(host, why, result.detail)
            )
        install.machine_config(runner, host, mc, sleep=self._sleep)
        logger.info(
            "machine-config on %s: registry mode %s", host, "on" if mc.registry_mode else "not set"
        )
        install.write_hook(runner, host, unset=_hook_unset(s), sleep=self._sleep)
        return result

    def _delete_quietly(self, sid: str) -> None:
        try:
            self.destroy(sid)
        except Exception as exc:  # noqa: BLE001 - the original failure is the one to report
            logger.warning(
                "could not delete sandbox %s after a failed launch (%s); delete it from "
                "Your crews",
                sid,
                type(exc).__name__,
            )

    # ── LaunchEngine: begin_signin ───────────────────────────────────────────
    def begin_signin(
        self, *, instance_id: str, profile: str, region: str, login_target: Any = None
    ) -> SandboxSigninHandle:
        if not sandbox_id_from_host(instance_id):
            return SandboxSigninHandle(error="not a sandbox ssh alias", target=login_target)
        return SandboxSigninHandle(instance_id, login_target, run=self.run)

    def run(self, host: str, script: str, *, timeout: float, env: Optional[dict] = None) -> Any:
        return self._ssh(host, script, timeout=timeout, env=env)

    # ── LaunchEngine: register ───────────────────────────────────────────────
    def register(self, *, instance_id: str, tag: str, profile: str, region: str) -> None:
        from kiro_crew.cloud.launch_job import RegistrationUnavailable

        try:
            self._write_row(profile, instance_id)
        except Exception as exc:
            raise RegistrationUnavailable(
                f"{instance_id} is running but could not be added to Remote Crew "
                f"({type(exc).__name__}). Delete it under Your crews, or add it by "
                f"hand with SSH host {instance_id}."
            ) from exc
        self._start_gateway(instance_id)
        if not sbx_remote.probe_health(self.run, instance_id, clock=self._clock, sleep=self._sleep):
            raise RegistrationUnavailable(
                f"{instance_id} is running and in Remote Crew, but its gateway did not answer "
                f"/api/health within {sbx_remote.PROBE_UP_TO_SECS} s. Retry Connect, or delete "
                "it under Your crews."
            )

    def _start_gateway(self, host: str) -> None:
        """Run the boot hook ``provision`` wrote; the health probe then decides. A hook that did not
        start the gateway raises :class:`RegistrationUnavailable` naming why and the way out."""
        from kiro_crew.cloud.launch_job import RegistrationUnavailable

        try:
            hook = install.run_hook(self.install_runner, host, sleep=self._sleep)
        except Exception as exc:
            raise RegistrationUnavailable(
                gateway_hook_failed_text(host, f"ssh: {type(exc).__name__}")
            ) from exc
        logger.info("gateway hook on %s: %s", host, hook.line())
        if hook.state == "needs_signin":
            raise RegistrationUnavailable(gateway_needs_signin_text(host))
        if hook.state == "not_kas_bundle":
            raise RegistrationUnavailable(gateway_not_kas_bundle_text(host))
        if hook.state not in ("started", "alive"):
            raise RegistrationUnavailable(gateway_hook_failed_text(host, hook.detail or hook.state))

    def _write_row(self, profile: str, instance_id: str, *, match_name: bool = True) -> None:
        """Add or update this lane's Remote Crew row for one environment. ``match_name=False``
        (the rows written from the List) takes the same-host path only, never the same-name
        branch, so a List-made row can't repoint another crew's row."""
        crew = validate_crew_name(profile)
        if not sandbox_id_from_host(instance_id):
            raise SandboxEngineError("bad_instance", "not a sandbox ssh alias")
        reg = self._registry()
        for row in reg.list():
            same_box = row.ssh_host == instance_id
            same_crew = match_name and (
                row.name == crew and getattr(row, "provisioner_id", "") == PROVISIONER_ID
            )
            if same_box or same_crew:
                fields: dict = dict(
                    connection_method="ssh",
                    ssh_host=instance_id,
                    remote_port=REMOTE_PORT,
                    remote_bin=REMOTE_BIN,
                )
                # A row first added another way (by hand) has no stamp, and with none the row
                # actions never draw on it: stamp it. Another provisioner's stamp is that lane's
                # claim on the row, so it is kept, and said.
                stamp = getattr(row, "provisioner_id", "") or ""
                if stamp in ("", PROVISIONER_ID):
                    fields["provisioner_id"] = PROVISIONER_ID
                else:
                    logger.warning(
                        "Remote Crew row %s for %s keeps provisioner %r; the sandbox row actions"
                        " won't draw on it",
                        row.id,
                        instance_id,
                        stamp,
                    )
                reg.update(row.id, **fields)
                return
        reg.add(
            name=crew,
            connection_method="ssh",
            ssh_host=instance_id,
            remote_port=REMOTE_PORT,
            remote_bin=REMOTE_BIN,
            provisioner_id=PROVISIONER_ID,
        )

    # ── LaunchEngine: teardown ───────────────────────────────────────────────
    def teardown(self, *, tag: str, profile: str, region: str) -> bool:
        try:
            rows = [
                r
                for r in self.guard.ledger()
                if r.get("displayName") == tag or crew_tag_of(r.get("displayName")) == tag
            ]
        except GuardRefused:
            logger.warning("sandbox ledger unreadable; no service call for %s", tag)
            return False
        if not rows:
            # No id, so no call: a name is never a key.
            logger.warning("no ledger entry for launch %s: no service call made", tag)
            return tag in self._never_created
        confirmed = True
        for row in rows:
            sid = str(row.get("execEnvironmentId") or "")
            try:
                if row.get("deletedAt"):
                    # Marked deleted right after the Delete call, before any read-back:
                    # a retry confirms (no new Delete) before it drops the row.
                    confirmed = self.confirm_gone(sid) and confirmed
                else:
                    confirmed = self.destroy(sid) and confirmed
            except (SandboxError, GuardRefused):
                logger.warning("could not confirm sandbox %s was deleted", sid, exc_info=True)
                confirmed = False
        return confirmed

    # ── Start Crew ───────────────────────────────────────────────────────────
    def start_crew(self, sandbox_id: str) -> tuple[bool, str]:
        """Start Crew on a recorded RUNNING environment whose gateway didn't answer: ONE exec of
        the closed-list ``hook-run`` (the box's own boot hook, idempotent), with no lone-255 retry
        and no gateway port read, so at most one StartSshSession. Returns (started, line). The
        caller's one Connect follows a start; nothing repeats without a new click. No exit
        starts a sign-in, a device code or an install."""
        r = self.install_runner.run(
            ssh_host_for(sandbox_id), install.COMMANDS_A[install.CMD_HOOK_RUN][1]
        )
        if r.rc == 0:
            return True, ""
        return False, start_crew_line(r.rc)

    # ── the one read, and adoption ───────────────────────────────────────────
    def crew_alive(self, sandbox_id: str) -> str:
        """The one ``crew-alive`` exec on a user-asked environment: ``found``, ``not_found`` or
        ``failed``. Its ssh takes one StartSshSession through a one-shot ticket, armed just
        before and disarmed after in a ``finally``. No retry, no hook, no install."""
        guard = self.guard
        guard.arm_ticket(sandbox_id)
        try:
            r = self.install_runner.run(
                ssh_host_for(sandbox_id), install.COMMANDS_E[install.CMD_CREW_ALIVE][1]
            )
        except Exception:
            logger.warning("crew-alive on %s failed to run", sandbox_id, exc_info=True)
            return "failed"
        finally:
            guard.disarm_ticket(sandbox_id)
        lines = (r.out or "").strip().splitlines()
        last = lines[-1].strip() if lines else ""
        if r.rc == 0 and last == install.CREW_UP:
            return "found"
        if r.rc == 0 and last == install.CREW_DOWN:
            return "not_found"
        return "failed"

    def adopt(self, sandbox_id: str, display_name: str) -> None:
        """Found: the box's Remote Crew row by :meth:`_write_row`'s same-host path, and the id in
        the guard's ``adopted`` list. Never the ledger.

        The guard is read first, at call time: a sign-in change since the click refuses before
        any write. A row this call wrote is removed again if noting ``adopted`` then fails, so
        no row is left for a box that isn't actionable."""
        guard = self.guard
        if not guard.can_adopt(sandbox_id, self.client.accept_fingerprint):
            raise GuardRefused(
                "not_own_id", "refused: the sign-in changed, or that sandbox was not asked here"
            )
        host = ssh_host_for(sandbox_id)
        had = any(r.ssh_host == host for r in self._registry().list())
        self._write_row(adopted_crew_name(display_name, sandbox_id), host, match_name=False)
        try:
            guard.adopt(sandbox_id)
        except Exception:
            if not had:
                self._drop_adopted_row(host)
            raise

    def rename_row(self, sandbox_id: str, name: str) -> tuple[bool, str]:
        """Give this lane's Remote Crew row for one environment (its host only, never another
        crew's row) the crew name *name*. Returns (changed, line): the line is "" when the row
        has that name now, else why not (no row here, or another crew's name)."""
        validate_crew_name(name)
        reg = self._registry()
        rows = reg.list()
        line = self._rename_refusal(sandbox_id, name, rows)
        if line:
            return False, line
        host = ssh_host_for(sandbox_id)
        mine = [
            r
            for r in rows
            if r.ssh_host == host and getattr(r, "provisioner_id", "") == PROVISIONER_ID
        ]
        changed = False
        for r in mine:
            if r.name != name:
                reg.update(r.id, name=name)
                changed = True
        return changed, ""

    def rename_refusal(self, sandbox_id: str, name: str) -> str:
        """Why this row can't take *name*, read before any service call: no lane row for the
        environment here, or another crew's row already has the name; "" when it can."""
        validate_crew_name(name)
        return self._rename_refusal(sandbox_id, name, self._registry().list())

    @staticmethod
    def _rename_refusal(sandbox_id: str, name: str, rows: list) -> str:
        host = ssh_host_for(sandbox_id)
        if not any(
            r.ssh_host == host and getattr(r, "provisioner_id", "") == PROVISIONER_ID for r in rows
        ):
            return RENAME_TEXT["no_row"]
        if any(r.name == name and r.ssh_host != host for r in rows):
            return RENAME_TEXT["taken"]
        return ""

    def _drop_adopted_row(self, ssh_host: str) -> None:
        try:
            reg = self._registry()
            for row in reg.list():
                if row.ssh_host == ssh_host:
                    (getattr(reg, "remove_cascade", None) or reg.remove)(row.id)
        except Exception:
            logger.warning(
                "could not remove the Remote Crew row for %s after a refused adoption; remove it "
                "under Settings -> Remote Crew",
                ssh_host,
                exc_info=True,
            )

    def destroy(self, sandbox_id: str) -> bool:
        """Delete an own environment, read it back, drop its rows.

        True only when ``GetSandbox`` answers 404 (or DELETED) and ``ListSandboxes`` no
        longer shows the id, and every row naming it is removed. The id must be in the
        ledger (the client refuses any other)."""
        self.client.delete_sandbox(sandbox_id)
        return self.confirm_gone(sandbox_id)

    def confirm_gone(self, sandbox_id: str) -> bool:
        """The read-back, with no Delete: Get answers 404 (or DELETED), List does not
        show the id, and the rows naming it are removed (kept when any step fails)."""
        try:
            got = self.client.get_sandbox(sandbox_id)
            gone = str(got.get("status")) == "DELETED"
        except SandboxError as exc:
            if not exc.not_found:
                raise
            gone = True
        if not gone:
            return False
        listed = {str(e.get("execEnvironmentId")) for e in self.client.list_sandboxes()}
        if sandbox_id in listed:
            return False
        return self._unregister(ssh_host_for(sandbox_id))

    def _unregister(self, ssh_host: str) -> bool:
        try:
            reg = self._registry()
            for row in reg.list():
                if row.ssh_host == ssh_host:
                    remove = getattr(reg, "remove_cascade", None) or reg.remove
                    remove(row.id)
        except Exception:
            logger.warning(
                "%s is deleted but its Remote Crew row could not be removed; remove it under "
                "Settings -> Remote Crew",
                ssh_host,
                exc_info=True,
            )
            return False
        return True


def _hook_unset(s: LaneSettings) -> tuple[str, ...]:
    """The configured names the boot hook clears: the image's credentials and its token variable."""
    return (*s.box_unset_env, *((s.box_token_env,) if s.box_token_env else ()))


def _reason(detail: str) -> str:
    """The ``reason=<x>`` an install result's detail names, or ""."""
    m = re.search(r"(?:^|\s)reason=(\S+)", detail or "")
    return m.group(1) if m else ""


def _install_exception_why(host: str, exc: Exception) -> tuple[str, str]:
    """(reason, detail) for an install step that raised instead of returning a result."""
    if isinstance(exc, install.InstallError):
        return exc.code, str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout", f"the install over ssh did not finish within {int(exc.timeout)} s"
    if isinstance(exc, OSError):
        return "ssh_failed", f"could not run ssh to {host} ({type(exc).__name__})"
    return "error", type(exc).__name__


#: The Create form's setting, in the provisioner's own ``size_key`` (the core passes it through,
#: validating it only for its built-in EC2 lane): ``""`` (the default) or ``idle=<minutes>``,
#: CreateSandbox's own optional ``idleTimeoutMinutes`` (15 to 10080). Absent is not sent: the
#: service's default applies.
CREATE_IDLE_RANGE = (15, 10080)

#: The service's answer to an idle timeout while idle stop is off for the account, matched by
#: its text, and the plain line the Launch form shows for it. Nothing is retried: Launch again
#: sends no timeout.
IDLE_SWITCH_MARK = "idleTimeoutMinutes isn't available"
CREATE_IDLE_OFF_TEXT = "Idle stop is off here, so the default applies. Launch again."
_idle_switch_off: list[float] = []


def note_idle_switch_off(at: Optional[float] = None) -> None:
    """The idle switch's 400 came (on Create): the form reads idle stop as off."""
    _idle_switch_off[:] = [time.time() if at is None else at]


def idle_switch_off_at() -> Optional[float]:
    """When a Create in this process last got the idle switch's 400, or None."""
    return _idle_switch_off[0] if _idle_switch_off else None


def create_options(size_key: str) -> Optional[int]:
    """The idle minutes from ``size_key``, or None when absent; a key, value or repeat outside
    the grammar refuses the launch before any call. A size_key with no ``=`` at all (a bare size
    word an older caller sends) carries no setting."""
    idle: Optional[int] = None
    if not size_key or "=" not in str(size_key):
        return idle
    seen: set[str] = set()
    for part in str(size_key).split(";"):
        key, sep, value = part.partition("=")
        if not sep or key in seen or not value.isdigit() or len(value) > 6:
            raise SandboxEngineError("bad_options", "The Create form's settings can't be read.")
        seen.add(key)
        n = int(value)
        if key == "idle" and CREATE_IDLE_RANGE[0] <= n <= CREATE_IDLE_RANGE[1]:
            idle = n
        else:
            raise SandboxEngineError("bad_options", "The Create form's settings can't be read.")
    return idle


def _engine_error(exc: Exception) -> "SandboxEngineError":
    """A stopped client reads as the plain line on the Launch form, with no code prefix and
    never the guard's own text; every other error keeps its code."""
    if isinstance(exc, ClientStopped):
        return SandboxEngineError(_code(exc), stopped_line(exc.by_service), plain=True)
    if (isinstance(exc, SandboxError) and is_policy_refusal(exc.op, exc.status, exc.kind)) or (
        isinstance(exc, GuardRefused) and exc.reason == "policy_denied"
    ):
        # the organization's policy refused this action, plainly, with no code
        return SandboxEngineError("policy_denied", POLICY_TEXT, plain=True)
    if isinstance(exc, SandboxError) and exc.kind == "not_configured":
        return SandboxEngineError("not_configured", exc.message, plain=True)
    if (
        isinstance(exc, SandboxError)
        and exc.op == "ListSandboxes"
        and ((exc.status or 0) >= 500 or exc.kind == "unreachable")
    ):
        # the List outage inside Create's preflight, plainly
        return SandboxEngineError(_code(exc), SERVICE_TEXT["try_later"], plain=True)
    return SandboxEngineError(_code(exc), str(exc))


def _code(exc: Exception) -> str:
    if isinstance(exc, GuardRefused):
        return exc.reason
    if isinstance(exc, SandboxStateError):
        return exc.kind
    if isinstance(exc, SandboxError):
        return exc.kind
    return "error"

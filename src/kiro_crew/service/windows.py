"""Windows service management through Task Scheduler.

The third backend beside :mod:`kiro_crew.service.linux` (systemd) and
:mod:`kiro_crew.service.macos` (launchd). It exists because the loop-stall
watchdog is designed to TERMINATE the gateway — ``faulthandler`` exits with
status 1 after writing its dump — and on Windows nothing brought it back, so a
designed-to-exit process was paired with no supervised lifetime. Reported
upstream as kirodotdev/KiroCrew#6590.

It follows launchd rather than systemd: the task is registered in the INVOKING
USER's scope and needs no elevation. A bare all-users logon trigger requires
administrator rights and fails with "Access is denied", which is easy to hit
and hard to read.

Four decisions are load-bearing, and each closes a way Task Scheduler's
defaults would have made a supervisor that silently does not supervise:

**The definition is XML, not ``/Create`` flags.** ``schtasks /Create`` exposes
no flag for ``RestartOnFailure`` or the battery settings below, and those are
the whole point of the task. ``/XML`` is the only route to them.
:mod:`kiro_crew.pod.windows` uses the flag form deliberately — its tasks are
one-shot pods that must NOT be restarted — so the two are not merged.

**``ExecutionTimeLimit`` is ``PT0S``.** The Task Scheduler default is three
days, after which it would terminate a perfectly healthy gateway. ``PT0S``
means no limit.

**Both battery settings are ``false``.** ``DisallowStartIfOnBatteries``
defaults to TRUE, so on a laptop the default task would simply not start, and
Task Scheduler reports that as a task that ran and did nothing — the exact
failure this module exists to prevent, wearing a green tick.

**``MultipleInstancesPolicy`` is ``IgnoreNew``.** A logon trigger can fire
while a gateway from a previous session is still up (fast user switching, a
remote session reconnect). Two gateways on one data home is a worse outcome
than a missed start, and the running one is the one to keep.

**The TRIGGER repeats, and that is what supervises the gateway.**
``RestartOnFailure`` does not, and the difference is the whole point of this
module. Measured against a live Task Scheduler: a task whose action exits 1 is
NOT restarted by it. The scheduler counts a non-zero exit as a run that
completed and records the code; only an action it could not LAUNCH is the
failure it restarts. A watchdog termination is precisely the former —
``faulthandler.dump_traceback_later(..., exit=True)`` calls ``_exit(1)`` — so
relying on ``RestartOnFailure`` alone leaves the gateway down, which is
kirodotdev/KiroCrew#6590 with a supervisor bolted on rather than fixed.

The logon trigger therefore carries a ``Repetition`` with an interval and NO
``Duration``, which repeats indefinitely: every minute the scheduler tries to
start the gateway. ``MultipleInstancesPolicy`` above is what makes that safe
rather than a fork bomb — while a healthy gateway holds the slot every tick is
dropped, and the first tick after it dies brings it back. ``RestartOnFailure``
is kept for the launch failure it does cover, and its count stays bounded
because that failure does not heal by retrying.

A repeating trigger costs one thing, and :func:`stop` pays it: a gateway an
operator stopped would otherwise be back within the minute. See there.

**This backend never parses ``schtasks`` output**, the same invariant
:mod:`kiro_crew.pod.windows` documents at length: both the CSV headers and the
``Status`` values are LOCALIZED, so a German or Japanese host would read as a
different state entirely. Only exit codes are consulted.

**Task Scheduler, not the SCM — and that is a scope decision, not a
preference.** kirodotdev/KiroCrew#7305 asks for headless Windows support and
proposes ``sc.exe``/SCM or an NSSM-style wrapper. The two are not
interchangeable:

* A real SCM service starts at BOOT and survives logout, but installing one
  needs ADMINISTRATOR rights, and Python is not a native service binary, so it
  also needs a wrapper (pywin32 or NSSM) to be one.
* A logon-triggered task needs no elevation at all, which is what makes it
  installable by the operator who hit kirodotdev/KiroCrew#6590 — but it starts at LOGON and does
  not survive logout.

This backend answers kirodotdev/KiroCrew#6590 (a workstation whose gateway
must come back after the watchdog kills it) and does NOT yet answer
kirodotdev/KiroCrew#7305 (a headless VM or server
that must come up before anyone signs in). Task Scheduler can reach that case
— a ``BootTrigger`` plus ``LogonType=S4U``, which avoids storing a password —
but S4U needs the "Log on as a batch job" right, so it is elevation by another
name and belongs in the same conversation as the SCM proposal rather than
being chosen here unilaterally.

One thing is deliberately NOT wired.
:func:`kiro_crew.service.controller.installed_unit_path` still answers ``None``
on this platform, so a host running this task does not claim the wider
managed-service watchdog budget. Claiming it would loosen a stall threshold on
the strength of an unvalidated backend, and the failure mode of that error is
the one this module exists to fix. It is a decision to revisit once the verbs
below have been proven against a real Task Scheduler, not an oversight.

.. warning::

   The document, the verbs and the supervision contract were exercised
   against a live Task Scheduler on Windows 10 Pro 19045 — the UTF-16
   definition is accepted, ``DOMAIN\\user`` resolves to the invoking
   principal, and the repeating trigger restarts a terminated action on the
   next minute while ``IgnoreNew`` drops the ticks a live one covers. Two
   things are still unproven and neither is reachable from a desktop:
   ``DisallowStartIfOnBatteries`` on a host that HAS a battery, and the
   document on a build whose Task Scheduler rejects UTF-16 rather than
   accepting it. The definition is kept at ``<data home>/service`` so an
   operator hitting either can read back exactly what was registered.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# saxutils.escape is an escaper, not a parser: str in, str out. An XML bomb or an
# external entity has nothing to reach, and defusedxml offers no replacement
# because it hardens parsers. Nothing in this module parses XML.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from xml.sax.saxutils import escape as _xml_escape

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import DASHBOARD_PORT
from kiro_crew.config.paths import config_dir

#: ``winreg`` where this platform has it, else ``None``. Typed ``Any`` for the
#: reason :mod:`kiro_crew.computer_use.launch_windows` gives: mypy runs with
#: ``--platform linux``, where the module's attributes are genuinely absent, so
#: naming them precisely would need the platform this is not analysed on.
_winreg: Any
try:  # Windows-only; this module is imported on every platform.
    import winreg as _winreg_module

    _winreg = _winreg_module
except ImportError:
    _winreg = None

logger = logging.getLogger(__name__)

#: Task Scheduler path. The leading folder keeps the task out of the crowded
#: root and matches how the pod backend namespaces its own.
TASK_FOLDER = r"\KiroCrew"
TASK_NAME = rf"{TASK_FOLDER}\gateway"

#: Explicit override hooks; ``None`` means resolve live. ``config_dir()``
#: reads ``KIROCREW_HOME`` on every call, so binding either of these at import
#: time would freeze whichever data home happened to be active when this module
#: was first imported — which outlives a pod's own home and outlives the
#: per-test isolation fixture, silently. Enforced by test_lazy_data_home_paths.
_TASK_XML_PATH: Path | None = None
_LOG_DIR: Path | None = None


def task_xml_path() -> Path:
    """Where the generated definition is kept for an operator to read back.

    Under the data home rather than a temp dir so an operator can see exactly
    what was registered, and so the file survives for ``kirocrew doctor`` to
    diff against the live task. It is NOT the file ``schtasks`` reads — see
    :func:`install`.
    """
    if _TASK_XML_PATH is not None:
        return _TASK_XML_PATH
    return config_dir() / "service" / "kirocrew-gateway.xml"


def log_dir() -> Path:
    """The data home's log directory."""
    return _LOG_DIR if _LOG_DIR is not None else config_dir() / "logs"


#: Supervision cadence, shared by the repeating trigger and the restart
#: budget. One minute is Task Scheduler's floor for a repetition interval, so
#: it is also the longest a gateway stays down after the watchdog kills it.
RESTART_INTERVAL = "PT1M"

#: Bounded budget for ``RestartOnFailure`` ONLY, which reaches an action that
#: could not be launched. Three attempts distinguishes "this host hiccuped"
#: from "this gateway cannot start", and stops rather than spinning on the
#: latter. It is not what recovers a terminated gateway — the trigger is.
RESTART_COUNT = 3

#: Ceiling on one ``schtasks`` call. The service is normally instant; a wait
#: past this means Task Scheduler is not answering, and a CLI verb that hangs
#: is worse than one that says the state is unknown.
_SCHTASKS_TIMEOUT_SECS = 30


class ServiceInstallError(RuntimeError):
    """Raised when the task could not be registered or removed."""


def _current_user_id() -> str:
    """The principal to run as, as ``DOMAIN\\user`` where a domain is known.

    Task Scheduler accepts a bare username, but a bare name on a
    domain-joined host can resolve to a different principal than the one
    invoking this. ``USERDOMAIN`` is present on every interactive Windows
    session; where it is absent (a service context, a stripped environment)
    the bare name is the honest answer rather than a guessed domain.
    """
    user = os.environ.get("USERNAME") or getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{user}" if domain else user


def gateway_command() -> tuple[str, str]:
    """``(command, arguments)`` Task Scheduler should execute.

    Prefers the installed ``kirocrew`` console script next to this
    interpreter, which is what the operator themselves runs. Falls back to
    ``<python> -m kiro_crew`` when the script is absent — an editable or
    unusual install — because a task pointing at a missing exe fails at logon
    with nothing but an exit code to explain it.

    The fallback carries ``-I``. ``-m`` puts the process's working directory
    first on ``sys.path``, and the task's working directory is the data home,
    which the agent can write: a ``kiro_crew.py`` or ``kiro_crew/`` planted
    there would be imported INSTEAD of the real package, by a task that runs
    as the operator at every logon and outside the agent's sandbox. ``-I``
    removes that entry.

    It also drops user site-packages, so an install that reached this
    interpreter only through ``pip install --user`` makes the task fail
    visibly at its first run rather than start. That is the intended trade:
    a loud failure an operator fixes by installing the console script, rather
    than a quiet path the agent can write into.
    """
    scripts = Path(sys.executable).parent
    for candidate in (scripts / "kirocrew.exe", scripts / "Scripts" / "kirocrew.exe"):
        if candidate.is_file():
            return str(candidate), "gateway"
    return sys.executable, "-I -m kiro_crew gateway"


def render_task_xml(
    *,
    user_id: str | None = None,
    command: str | None = None,
    arguments: str | None = None,
    working_dir: str | None = None,
) -> str:
    """Render the Task Scheduler definition.

    Pure and fully injectable so the whole document is testable off Windows.
    """
    if user_id is None:
        user_id = _current_user_id()
    if command is None or arguments is None:
        resolved_cmd, resolved_args = gateway_command()
        command = resolved_cmd if command is None else command
        arguments = resolved_args if arguments is None else arguments
    if working_dir is None:
        working_dir = str(config_dir())

    e = _xml_escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{e(user_id)}</Author>
    <Description>Kiro Crew gateway. Restarts the gateway after the loop-stall watchdog terminates it.</Description>
    <URI>{e(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{e(user_id)}</UserId>
      <Repetition>
        <Interval>{RESTART_INTERVAL}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>{RESTART_INTERVAL}</Interval>
      <Count>{RESTART_COUNT}</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(command)}</Command>
      <Arguments>{e(arguments)}</Arguments>
      <WorkingDirectory>{e(working_dir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def write_task_xml(contents: str, path: Path | None = None) -> Path:
    """Write the definition where ``schtasks /XML`` will read it.

    UTF-16 with a BOM, matching Task Scheduler's own exports and the
    ``encoding="UTF-16"`` the document declares.

    Published through :func:`atomic_write` so a concurrent reader never sees a
    half-written definition, and so the temp file is UNIQUE and ``O_EXCL``. A
    temp name derived from the PID would be predictable, and one caller writes
    into the data home, which the agent can write: a symlink waiting at that
    name would be followed by a plain write and would truncate its target.
    The content is handed over as bytes because ``utf-16`` is not a mode
    ``atomic_write`` writes text in, and bytes are never newline-translated.
    """
    target = path or task_xml_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, contents.encode("utf-16"))
    return target


def schtasks_bin() -> str | None:
    """Absolute path of ``schtasks.exe``, or ``None`` when unavailable.

    Resolved through the trusted-system-path table rather than ``PATH``: a
    writable directory earlier on ``PATH`` must not be able to supply the
    binary that registers a task running as this user at every logon.
    """
    return platform_compat.trusted_system_bin("schtasks")


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    """The single chokepoint for talking to Task Scheduler."""
    exe = schtasks_bin()
    if exe is None:
        raise ServiceInstallError(
            "schtasks.exe was not found in a trusted system directory, so the "
            "gateway cannot be supervised on this host."
        )
    try:
        return subprocess.run(
            [exe, *args],
            capture_output=True,
            timeout=_SCHTASKS_TIMEOUT_SECS,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        # Raised, not returned, so no caller can mistake "the scheduler did
        # not answer" for a result. Every controller branch that reaches this
        # handles ServiceInstallError; a TimeoutExpired or an OSError would
        # walk past those handlers and reach the operator as a traceback.
        raise ServiceInstallError(
            f"schtasks {args[0] if args else ''} did not answer within "
            f"{_SCHTASKS_TIMEOUT_SECS}s, so the task's state is unknown."
        ) from exc
    except OSError as exc:
        raise ServiceInstallError(f"could not run schtasks: {exc}") from exc


#: The environment the OTHER backends capture at install time and bake into
#: the unit (see :func:`kiro_crew.service.common.service_environment`). Task
#: Scheduler's definition has no environment block, so this backend cannot
#: capture them — it refuses instead, which is why the list lives here.
_MUST_BE_PERSISTED = ("KIROCREW_HOME", "KIROCREW_PORT")


def _persisted_override(name: str) -> str | None:
    """*name* as a NEW logon session would see it, or ``None``.

    Read from the user's persistent environment rather than this process's,
    because the two are what diverge: a shell that exported the variable for
    itself passes it to ``kirocrew service install`` and to nothing else.

    Raises when the registry cannot be read at all, because "I could not look"
    must not read as "nothing is set".
    """
    if _winreg is None:  # not Windows; the caller gates on this anyway
        return None
    try:
        with _winreg.OpenKey(_winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = _winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        # The value is simply not set. That is an answer, not a failure.
        return None
    except OSError as exc:
        # Anything else means the persistent environment could not be read, so
        # whether the task would inherit this variable is UNKNOWN. Failing
        # closed here is the whole point of the caller.
        raise ServiceInstallError(
            f"could not read the persistent user environment to check {name}: {exc}"
        ) from exc
    return str(value) if value else None


def _refuse_an_environment_the_task_cannot_inherit() -> None:
    """Refuse to install a task that would supervise a DIFFERENT gateway.

    Task Scheduler's definition carries no environment block, so the task
    inherits whatever the logon session has. A variable set only in the
    installing shell is therefore not carried, and the two that matter both
    fail silently: ``KIROCREW_HOME`` brings the supervised gateway up on the
    DEFAULT data home, under a different security policy and different
    sessions, and ``KIROCREW_PORT`` brings it up on the default port, which is
    a crash loop on the host where the operator moved it because 5476 was
    already taken.

    systemd and launchd capture both at install time
    (:func:`kiro_crew.service.common.service_environment`). This backend
    cannot, so it refuses rather than installing something that reports
    success and supervises the wrong thing.
    """
    if not platform_compat.IS_WINDOWS:
        return
    for name in _MUST_BE_PERSISTED:
        active = os.environ.get(name) or ""
        persisted = _persisted_override(name) or ""
        if active == persisted:
            continue
        if active:
            raise ServiceInstallError(
                f"{name}={active} is set for this process but the persistent "
                f"user environment has {persisted or 'no value'}, and a "
                "scheduled task inherits only the latter, so installing now "
                "would supervise a gateway that does not use it. Persist it "
                "first, then reinstall:" + chr(10) + f'    setx {name} "{active}"'
            )
        # The other direction, which is just as wrong and easier to miss: the
        # persistent environment carries a value this process does not, so the
        # task would supervise a gateway configured differently from the one
        # the operator is installing from.
        raise ServiceInstallError(
            f"{name}={persisted} is set in your persistent user environment "
            "but not for this process, so the supervised gateway would not "
            "match the one you are installing from. Run the install from a "
            f"session that has {name} set, or clear it:" + chr(10) + f'    setx {name} ""'
        )


def install() -> Path:
    """Register (or replace) the gateway task. Returns the definition's path.

    ``/F`` replaces an existing task rather than failing, so a reinstall after
    an upgrade picks up a changed command without an uninstall first.

    The definition ``schtasks`` READS is written to a fresh private directory
    and deleted immediately, never to the data home. The data home is
    agent-writable, so a definition parked there at a predictable path is a
    window in which the action ``schtasks`` registers is not the action this
    function rendered — and what it registers runs as the operator at every
    logon, outside the sandbox. The copy under the data home is written after
    registration, for an operator or ``kirocrew doctor`` to read back, and is
    never the file that is registered.
    """
    _refuse_an_environment_the_task_cannot_inherit()
    document = render_task_xml()
    log_dir().mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="kirocrew-task-"))
    try:
        created = _schtasks(
            "/Create",
            "/TN",
            TASK_NAME,
            "/XML",
            str(write_task_xml(document, staging / "t.xml")),
            "/F",
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if created.returncode != 0:
        # The message is NOT parsed, only surfaced: it is localized, and the
        # operator reading it is the one who can act on it.
        raise ServiceInstallError(
            f"schtasks /Create rc={created.returncode}: "
            f"{(created.stderr or created.stdout or '').strip()}"
        )
    return write_task_xml(document)


def uninstall() -> None:
    """Remove the task. A task that is already absent is not an error.

    Absence is established BEFORE the delete, not inferred from it failing.
    Reading a second non-zero result as proof of absence is how a denied
    delete and a denied query combined into a reported removal that did not
    happen, leaving a task that restarts the gateway every minute. With the
    order this way round, every non-zero ``/Delete`` is a real failure.
    """
    if not is_installed():
        return
    deleted = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    if deleted.returncode != 0:
        raise ServiceInstallError(
            f"schtasks /Delete rc={deleted.returncode}: "
            f"{(deleted.stderr or deleted.stdout or '').strip()}"
        )


def is_installed() -> bool:
    """Whether Task Scheduler holds our task.

    The EXIT CODE answers this; the output is localized and never read.

    An INDETERMINATE query -- schtasks missing, timed out, or refused by the
    OS -- propagates rather than answering ``False``. Absence is a conclusion,
    and swallowing it here let :func:`uninstall` pair a denied delete with a
    denied query and report a removal that did not happen, leaving a task that
    restarts the gateway every minute behind a CLI that said it was gone.
    """
    return _schtasks("/Query", "/TN", TASK_NAME).returncode == 0


def start() -> None:
    """Start the task now, without waiting for a logon.

    Re-enables first, because :func:`stop` disables the task to make an
    operator's stop hold against the repeating trigger. Enabling a task that
    is already enabled is accepted, so this costs one call and no branch.
    """
    enabled = _schtasks("/Change", "/TN", TASK_NAME, "/ENABLE")
    if enabled.returncode != 0:
        raise ServiceInstallError(
            f"schtasks /Change /ENABLE rc={enabled.returncode}: "
            f"{(enabled.stderr or enabled.stdout or '').strip()}"
        )
    run = _schtasks("/Run", "/TN", TASK_NAME)
    if run.returncode != 0:
        raise ServiceInstallError(
            f"schtasks /Run rc={run.returncode}: " f"{(run.stderr or run.stdout or '').strip()}"
        )


def stop() -> None:
    """Stop the gateway and stand the supervisor down until :func:`start`.

    ``/End`` alone would not hold. The trigger repeats every minute, so a
    gateway the operator stopped would be back inside one — the supervisor
    cannot tell an operator's stop from the watchdog's, and it must not, or it
    would not recover the watchdog's. Disabling the task is how the operator
    says which one it was, and it is this backend's ``systemctl stop``.

    ``/Change /DISABLE`` runs FIRST and its failure IS an error: it is
    idempotent, so it does not refuse an already-disabled task, and a non-zero
    result means the trigger is still armed to undo this stop within the
    minute while the operator believes the gateway is down. ``/End`` then
    refuses only when nothing is running, which is the state the caller asked
    for, so that one is tolerated.
    """
    # DISABLE FIRST. The trigger fires every minute, so a tick landing between
    # an /End and a /DISABLE starts a gateway the stop has already accounted
    # for, and the CLI reports a stop while that one keeps serving. Disarming
    # before ending closes the window; the reverse order leaves one a minute
    # wide, every time.
    disabled = _schtasks("/Change", "/TN", TASK_NAME, "/DISABLE")
    if disabled.returncode != 0:
        raise ServiceInstallError(
            f"schtasks /Change /DISABLE rc={disabled.returncode}: "
            f"{(disabled.stderr or disabled.stdout or '').strip()}"
        )
    _schtasks("/End", "/TN", TASK_NAME)


def is_active() -> bool:
    """Whether a gateway is actually serving.

    Deliberately NOT ``schtasks /Query``'s ``Status`` column. That column is
    LOCALIZED, so matching it would answer correctly on an English host and
    wrongly everywhere else — and the failure would be silent, which is the
    class of defect this module exists to remove.

    It also answers a different question than the caller asks. The scheduler
    knows whether it launched something; the caller wants to know whether a
    gateway is up. Those diverge exactly when it matters: a task that started
    and whose gateway then wedged reads as running. A connect to the dashboard
    port is the same evidence :func:`kiro_crew.snapshot._is_gateway_running`
    uses, it is locale-independent, and it is answered by the gateway itself.
    """
    port = DASHBOARD_PORT
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def restart() -> bool:
    """Stop the running instance and start it again. True iff accepted.

    Task Scheduler has no atomic restart verb, so this is ``/End`` then
    ``/Run``. Only ``/Run``'s exit code decides the answer: ``/End`` on an
    instance that already exited is a refusal that says nothing about whether
    the restart will work, and treating it as failure would report a
    successful restart as a failed one.
    """
    _schtasks("/End", "/TN", TASK_NAME)
    if _schtasks("/Change", "/TN", TASK_NAME, "/ENABLE").returncode != 0:
        # A restart that could not re-arm the trigger has not restarted
        # anything that will survive the next termination.
        return False
    return _schtasks("/Run", "/TN", TASK_NAME).returncode == 0


def status() -> str:
    """A human-readable status line.

    Composed from facts this module can establish without reading localized
    output: whether Task Scheduler holds the task, and whether a gateway is
    answering. ``schtasks``'s own status prose is never echoed, because an
    operator comparing two hosts should not see two different vocabularies for
    the same state.
    """
    installed = is_installed()
    active = is_active()
    if not installed:
        return f"not installed (no task at {TASK_NAME})"
    return f"installed at {TASK_NAME}; gateway is {'running' if active else 'not running'}"

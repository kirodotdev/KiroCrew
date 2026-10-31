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
different state entirely. Exit codes are consulted, plus one exact match of
this task's own path in the task list (:func:`is_installed`), which is the
task's name and is never translated.

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
import http.client
import logging
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

# saxutils.escape is an escaper, not a parser: str in, str out. An XML bomb or an
# external entity has nothing to reach, and defusedxml offers no replacement
# because it hardens parsers. Nothing in this module parses XML.
# nosemgrep: python.lang.security.use-defused-xml.use-defused-xml
from xml.sax.saxutils import escape as _xml_escape

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.port_resolution import resolve_client_port

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


def _user_scripts_dir() -> Path | None:
    """This interpreter's per-user Scripts directory, or ``None``.

    The ``nt_user`` install scheme is where ``pip install --user`` writes
    console scripts on Windows (``%APPDATA%\\Python\\PythonXY\\Scripts``).
    ``None`` when the scheme is unknown to this interpreter.
    """
    try:
        path = sysconfig.get_path("scripts", "nt_user")
    except KeyError:
        return None
    return Path(path) if path else None


def gateway_command() -> tuple[str, str]:
    """``(command, arguments)`` Task Scheduler should execute.

    Prefers the installed ``kirocrew`` console script next to this
    interpreter, which is what the operator themselves runs, then the one in
    this interpreter's per-user Scripts directory, which is where
    ``pip install --user`` puts it. Falls back to ``<python> -m kiro_crew``
    when neither exists — an editable or unusual install — because a task
    pointing at a missing exe fails at logon with nothing but an exit code to
    explain it.

    The fallback carries ``-I``. ``-m`` puts the process's working directory
    first on ``sys.path``, and the task's working directory is the data home,
    which the agent can write: a ``kiro_crew.py`` or ``kiro_crew/`` planted
    there would be imported INSTEAD of the real package, by a task that runs
    as the operator at every logon and outside the agent's sandbox. ``-I``
    removes that entry.

    It also drops user site-packages, which is why the per-user Scripts
    directory is checked before falling back: a ``pip install --user`` puts
    both the package and its console script there, and the console script
    imports the package from the user site as the operator's own shell does.
    The fallback is reached only when no console script exists anywhere this
    interpreter installs one.
    """
    scripts = Path(sys.executable).parent
    candidates = [scripts / "kirocrew.exe", scripts / "Scripts" / "kirocrew.exe"]
    user_scripts = _user_scripts_dir()
    if user_scripts is not None:
        candidates.append(user_scripts / "kirocrew.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate), "gateway --no-open"
    # `--no-open` for the same reason as the systemd unit and the launchd
    # plist: logon and watchdog runs are unattended, and each would otherwise
    # open another dashboard tab in the operator's browser.
    return sys.executable, "-I -m kiro_crew gateway --no-open"


def render_task_xml(
    *,
    user_id: str | None = None,
    command: str | None = None,
    arguments: str | None = None,
    working_dir: str | None = None,
    start_boundary: str | None = None,
) -> str:
    """Render the Task Scheduler definition.

    Pure and fully injectable so the whole document is testable off Windows.

    Two triggers carry the same repetition. The ``TimeTrigger`` starts at
    install time, so supervision is armed the moment the task is registered:
    ``/Run`` launches the action without firing any trigger, and a repetition
    hung only on the ``LogonTrigger`` would stay unarmed until the operator
    next signed in, leaving a watchdog termination unrecovered until then.
    The ``LogonTrigger`` re-arms it after every later sign-in. ``IgnoreNew``
    makes the two harmless together: a tick from either while a gateway holds
    the slot is dropped.
    """
    if start_boundary is None:
        # Local time with no offset: Task Scheduler reads an offset-less
        # boundary as local, so it fires on this host's clock whatever the zone.
        start_boundary = datetime.now().replace(microsecond=0).isoformat()
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
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{e(start_boundary)}</StartBoundary>
      <Repetition>
        <Interval>{RESTART_INTERVAL}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
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
# KIROCREW_KIRO_BIN too: service_environment() bakes it into the systemd and
# launchd units, so a shell-transient pin here would supervise a gateway that
# resolves a different kiro binary.
_MUST_BE_PERSISTED = ("KIROCREW_HOME", "KIROCREW_PORT", "KIROCREW_KIRO_BIN")


def _persisted_override(name: str) -> str | None:
    """*name* as a NEW logon session would see it, or ``None``.

    Read from the user's persistent environment rather than this process's,
    because the two are what diverge: a shell that exported the variable for
    itself passes it to ``kirocrew service install`` and to nothing else.

    A logon session's environment is the machine-wide one with the user's on
    top, each ``REG_EXPAND_SZ`` value expanded, so this reads the user key
    first, falls back to the machine key, and expands before answering:
    comparing an unexpanded ``%USERPROFILE%\\crew`` against this process's
    expanded value would refuse an install the task inherits correctly.

    Raises when the registry cannot be read at all, because "I could not look"
    must not read as "nothing is set".
    """
    if _winreg is None:  # not Windows; the caller gates on this anyway
        return None
    for root, subkey in _PERSISTENT_ENVIRONMENT_KEYS:
        try:
            with _winreg.OpenKey(getattr(_winreg, root), subkey) as key:
                value, kind = _winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            # Not set at this level. That is an answer, not a failure.
            continue
        except OSError as exc:
            # Anything else means the persistent environment could not be read,
            # so whether the task would inherit this variable is UNKNOWN.
            # Failing closed here is the whole point of the caller.
            raise ServiceInstallError(
                f"could not read the persistent environment to check {name}: {exc}"
            ) from exc
        if kind == _winreg.REG_EXPAND_SZ:
            value = _winreg.ExpandEnvironmentStrings(value)
        return str(value) if value else None
    return None


#: Where a new logon session's environment comes from, highest precedence
#: first. Roots are ``winreg`` attribute names, resolved at call time.
_PERSISTENT_ENVIRONMENT_KEYS = (
    ("HKEY_CURRENT_USER", "Environment"),
    ("HKEY_LOCAL_MACHINE", r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
)


def _refuse_a_relative_data_home() -> None:
    """Refuse a ``KIROCREW_HOME`` that names a different home from another directory.

    ``config_dir()`` resolves a relative override against the CURRENT
    directory, and the task starts in the data home this process resolved. So
    the scheduled gateway resolves the same string again from inside that home
    and lands in a nested, empty one, with the default policy and none of the
    operator's sessions. Equal strings in the shell and in the persistent
    environment do not catch it, because the string is not what differs. A
    drive-relative ``C:crew`` or rooted ``\\crew`` is not absolute either.
    """
    raw = os.environ.get("KIROCREW_HOME") or ""
    if raw and not Path(raw).expanduser().is_absolute():
        raise ServiceInstallError(
            f"KIROCREW_HOME={raw} is a relative path, and the scheduled task "
            "would resolve it from inside the data home rather than from here, "
            "so it would supervise a gateway on a different home. Set it to an "
            "absolute path, persist it, then reinstall:"
            + chr(10)
            + f'    setx KIROCREW_HOME "{os.path.abspath(os.path.expanduser(raw))}"'
        )


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
    _refuse_a_relative_data_home()
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


def install() -> None:
    """Register (or replace) the gateway task.

    The readback copy lands at :func:`task_xml_path`. Returning that path here
    would make ``install`` a Path factory to ``test_lazy_data_home_paths``,
    which matches bare call names and would then flag every unrelated
    ``X = something.install()`` in the tree.

    ``/F`` replaces an existing task rather than failing, so a reinstall after
    an upgrade picks up a changed command without an uninstall first.

    The definition ``schtasks`` READS is written to a fresh private directory
    and deleted immediately, never to the data home. The data home is
    agent-writable, so a definition parked there at a predictable path is a
    window in which the action ``schtasks`` registers is not the action this
    function rendered — and what it registers runs as the operator at every
    logon, outside the sandbox. The copy under the data home is for an operator
    or ``kirocrew doctor`` to read back, and is never the file that is
    registered.

    **Registration goes LAST, after every step that can fail.** The data-home
    readback copy is written first, so an ``OSError`` on it -- ``ENOSPC``, a
    read-only or replaced path -- reaches the operator with nothing registered.
    Reverse the two and that same error escapes past the controller (which
    handles only :class:`ServiceInstallError`) with the task ALREADY registered
    and running the gateway at the operator's next logon. A failed install that
    leaves a live logon task behind is the one outcome this function must not
    have, so the order removes that state rather than compensating for it:
    no window exists in which a task is registered and the install can still
    fail.

    Rolling the registration back is the alternative, and it is worse.
    ``/Create /F`` REPLACES any prior task, so the thing a rollback would
    restore is already gone by the time it runs; deleting leaves an operator
    who had working supervision with none, and restoring the prior definition
    means reading it back out of Task Scheduler through :func:`_schtasks`,
    which decodes as UTF-8 -- and ``/Query /XML`` answers in UTF-16.

    A ``/Create`` that fails leaves the readback copy describing a task that
    was never registered, so the prior copy is put back (or removed when there
    was none). That restore is best-effort: it is an inert file, and it must
    never replace the scheduler's own error as the reported cause.
    """
    _refuse_an_environment_the_task_cannot_inherit()
    document = render_task_xml()
    readback = task_xml_path()
    prior = _read_prior_readback(readback)
    log_dir().mkdir(parents=True, exist_ok=True)
    # Before /Create: an OSError here has registered nothing, so it reaches the
    # operator with the system in the state they started in.
    try:
        write_task_xml(document)
    except OSError as exc:
        raise ServiceInstallError(f"could not write {readback}: {exc}") from exc
    staging: Path | None = None
    try:
        staging = Path(tempfile.mkdtemp(prefix="kirocrew-task-"))
        definition = write_task_xml(document, staging / "t.xml")
    except OSError as exc:
        # Nothing is registered yet, so the readback copy written above now
        # describes a task that does not exist.
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        _restore_readback(readback, prior)
        raise ServiceInstallError(f"could not stage the task definition: {exc}") from exc
    try:
        created = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(definition), "/F")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if created.returncode != 0:
        _restore_readback(readback, prior)
        # The message is NOT parsed, only surfaced: it is localized, and the
        # operator reading it is the one who can act on it.
        raise ServiceInstallError(
            f"schtasks /Create rc={created.returncode}: "
            f"{(created.stderr or created.stdout or '').strip()}"
        )


#: Ceiling on the prior readback copy kept for a rollback. A rendered
#: definition is a few kilobytes; anything near this is not one.
_READBACK_MAX_BYTES = 256 * 1024


def _read_prior_readback(path: Path) -> bytes | None:
    """The existing readback copy, kept so a failed registration can put it back.

    The copy sits in the data home, which the agent can write, and
    :func:`_restore_readback` republishes these bytes as an ordinary file. A
    plain read would follow a link planted at that name and copy whatever it
    points at, a credential file included, out from behind the sensitive-path
    boundary. So the read refuses a link or junction at the name, a hardlinked
    inode, anything outside the readback's own directory, a sensitive path and
    an oversized file, and an existing copy it refuses stops the install
    before anything is written or registered.
    """
    if not os.path.lexists(path):
        return None
    from kiro_crew.hook_runtime.safe_reads import safe_read_file_bytes_nolink
    from kiro_crew.hooks import FileTooLargeError

    try:
        data = safe_read_file_bytes_nolink(
            str(path), str(path.parent), max_bytes=_READBACK_MAX_BYTES
        )
    except FileTooLargeError:
        data = None
    if data is None:
        raise ServiceInstallError(
            f"{path} exists but is not a plain file this install wrote (a link, a "
            "hardlink, or an oversized file), so it was left untouched and nothing "
            "was registered. Remove it, then reinstall."
        )
    return data


def _restore_readback(path: Path, prior: bytes | None) -> None:
    """Undo the readback copy after a registration that did not happen.

    Best-effort by construction: every caller is already raising the
    scheduler's own failure, and a secondary ``OSError`` from tidying an inert
    file must not displace it. The residual if this does fail is a stale
    definition file, never a live task.
    """
    try:
        if prior is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, prior)
    except OSError:
        pass


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

    ``/Query /TN`` exits non-zero both for a task that does not exist and for
    one the scheduler refused to describe, and only localized prose tells them
    apart. So a non-zero answer is checked against the full task list, whose
    TaskName column is the task's own path and is never translated: when the
    list itself is refused the state is unknown and this raises, and when it
    names our task the targeted query was refused and this raises too. Only a
    list that answers and does not name the task is absence.
    """
    if _schtasks("/Query", "/TN", TASK_NAME).returncode == 0:
        return True
    listing = _schtasks("/Query", "/FO", "CSV", "/NH")
    if listing.returncode != 0:
        raise ServiceInstallError(
            f"schtasks /Query rc={listing.returncode}: could not establish whether "
            f"{TASK_NAME} is registered: {(listing.stderr or listing.stdout or '').strip()}"
        )
    if f'"{TASK_NAME.lower()}"' in (listing.stdout or "").lower():
        raise ServiceInstallError(
            f"{TASK_NAME} is registered but Task Scheduler refused to query it"
        )
    return False


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

    The port comes from the same resolver the other client verbs use, so a
    ``KIROCREW_PORT`` or a ``dashboard.url`` port is probed where the gateway
    actually binds, not at the import-time default.
    """
    port = resolve_client_port(None)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def is_serving() -> bool:
    """Whether THIS install's gateway is up and ready, not just something on the port.

    :func:`is_active` is a TCP connect, which is the right question for a stop
    (is anything still holding the port) and the wrong one for ``status`` and
    ``restart``: any other program on that port answers it, and so does a
    gateway still starting or already refusing to start. Two facts together
    answer this one. The gateway records its pid in the data home's run marker
    for the port, which only it can write, and that pid must still exist; and
    ``/api/ready`` must answer 200, which it does only once the gateway is
    serving and never while it shuts down. Both are locale-independent.
    """
    port = resolve_client_port(None)
    pid = run_marker.read_pid(port)
    if pid is None or not platform_compat.pid_exists(pid):
        return False
    return _ready_status(port) == 200


def _ready_status(port: int) -> int:
    """HTTP status of the loopback ``/api/ready``, ``0`` when nothing usable answers."""
    url = f"http://127.0.0.1:{port}/api/ready"
    try:
        # Loopback-only probe; the URL is built here from a resolved port and
        # is never caller-supplied. /api/ready needs no auth, so nothing secret
        # is sent to whatever answers.
        with loopback_urlopen(url, timeout=3) as resp:  # nosemgrep
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0


#: How long a restart waits for the relaunched gateway to report ready. The
#: same default the foreground restart waits for a replacement.
_READY_TIMEOUT_SECS = 60.0
_READY_POLL_SECS = 0.5


def wait_until_serving(timeout: float = _READY_TIMEOUT_SECS) -> bool:
    """Whether :func:`is_serving` turns true within *timeout* seconds.

    Probed at least once, so a zero timeout reports what is there now.
    """
    deadline = time.monotonic() + timeout
    while not is_serving():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL_SECS)
    return True


#: How long :func:`restart` waits for the port to close after ``/End`` before
#: concluding the gateway serving it is not this task's.
_PORT_RELEASE_TIMEOUT_SECS = 10.0
_PORT_RELEASE_POLL_SECS = 0.25


def _port_released(timeout: float = _PORT_RELEASE_TIMEOUT_SECS) -> bool:
    """Whether the dashboard port stops answering within *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while is_active():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PORT_RELEASE_POLL_SECS)
    return True


def restart() -> bool | None:
    """Stop the running instance and start it again.

    True when the task was re-armed and ``/Run`` accepted, False when the
    scheduler refused, and ``None`` when the gateway serving the port is not
    this task's: the caller then restarts that gateway itself. True is not yet
    a restart: the controller waits on :func:`wait_until_serving` before it
    reports one.

    Task Scheduler has no atomic restart verb, so this is ``/End`` then
    ``/Run``. ``/End`` on an instance that already exited is a refusal that
    says nothing about whether the restart will work, so its exit code is not
    read. What IS read is the port: registration is not ownership, and a
    gateway the operator started in the foreground beside a stopped task keeps
    answering after ``/End``. Re-enabling and running the task then would
    report a restart that never reached that gateway, and re-arm a supervisor
    the operator had stood down. Nothing is changed in that case.
    """
    serving = is_active()
    _schtasks("/End", "/TN", TASK_NAME)
    if serving and not _port_released():
        return None
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
    active = is_serving()
    if not installed:
        return f"not installed (no task at {TASK_NAME})"
    return f"installed at {TASK_NAME}; gateway is {'running' if active else 'not running'}"

"""Gateway restart target selection through the composed lifecycle provider."""

from __future__ import annotations

import errno
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

from kiro_crew import dep_sync, platform_compat
from kiro_crew.platform.context import current_context
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.update_ownership import Reentry, ReentryVerdict


def resolve_restart_launcher() -> str | None:
    """Validate the edition launcher before draining any live sessions.

    Imported by restart consumers before an update can retire their import tree.
    None alone opts into the core's existing Python/managed-venv resolver. A bad
    explicit target or a provider error refuses restart, never falls back to A.
    This is an availability check, not a new authorization boundary: the trusted
    composition root supplies the provider, not request/config/environment data.
    """
    launcher = current_context().gateway_lifecycle.restart_launcher()
    if launcher is None:
        return None
    if not isinstance(launcher, str) or not launcher or "\0" in launcher:
        raise ValueError("Cannot restart: invalid gateway launcher path")
    path = Path(launcher)
    if not path.is_absolute() or not path.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError("Cannot restart: gateway launcher must be an absolute executable file")
    if platform_compat.IS_WINDOWS and path.suffix.lower() != ".exe":
        raise ValueError("Cannot restart: gateway launcher must be a native Windows executable")
    # Do not resolve symlinks: dispatchers can select the app from this basename.
    return launcher


#: Budget of one re-entry check, step by step. It must stay inside the
#: stale-asset watchdog's own bound on the whole check
#: (``_REENTRY_CHECK_TIMEOUT_SECS``): two ``systemctl show`` queries (one per
#: scope) and two import probes (the supervisor's command, then a live target's).
_SYSTEMCTL_TIMEOUT_SECS = 3.0
_IMPORT_PROBE_TIMEOUT_SECS = 10.0
#: errnos that mean this user cannot reach a path; the service runs as this
#: user, so its exec cannot reach it either.
_UNREACHABLE_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
#: errnos from running an interpreter that prove the supervisor's exec of it
#: would fail too, as opposed to a check that merely could not run.
_EXEC_FAILS_ERRNOS = _UNREACHABLE_ERRNOS | {errno.ENOEXEC}
#: errnos from a stat that mean the path is not there.
_MISSING_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
#: Leading bytes of an image the kernel runs itself: ELF, then Mach-O (32/64-bit,
#: either byte order) and a universal binary.
_NATIVE_MAGICS = (
    b"\x7fELF",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
)
_EXEC_START_PATH = re.compile(r"\bpath=(.*?) ; argv\[\]=(.*?) ; ")
_ENVIRONMENT_FILE = re.compile(r"(\S+) \(ignore_errors=(yes|no)\)")


class _Inconclusive(Exception):
    """The supervisor's command could not be established; nothing is proven."""


def supervisor_reentry() -> ReentryVerdict:
    """Whether the service manager that relaunches this gateway could start it again.

    The stale-asset watchdog's shutdown is a request to be relaunched: it exits
    with :data:`~kiro_crew.dashboard.stale_asset_watchdog.STALE_ASSET_EXIT_CODE`
    and the service manager runs its OWN command (the systemd unit's
    ``ExecStart`` as systemd has it loaded, the launchd agent's launcher). So
    that command is what is tested, not this process's interpreter: an apply
    that pruned the old tree while the command's stable link points at a healthy
    new one relaunches fine. The command must exist through its links and be
    executable, and when it is a Python script its interpreter must import the
    gateway's entry point under the unit's own environment, which a venv killed
    mid-install fails. A live target the relaunch would hop into is tested the
    same way. Not launched by a generated service definition (a foreground run,
    the desktop app, a container), nothing relaunches through a command this can
    read, so there is nothing to refuse.

    A failure that proves the exec would fail (missing, not executable, an
    interpreter that will not run or import) is a refusal; anything that cannot
    be established (an unreadable definition, a shell wrapper, a stale unit, a
    probe that does not answer) is inconclusive. Blocking (it queries the service
    manager and runs bounded probes): run it off the event loop.
    """
    from kiro_crew.config.loader import launched_as_managed_service

    if not launched_as_managed_service():
        return ReentryVerdict(Reentry.REENTERABLE)
    try:
        command, args, env = _supervisor_launch()
        # A wrapper the supervisor execs (the launchd launcher, ``env``) must
        # itself be runnable before the program it leads to is judged.
        wrapper = _wrapper_target(command, args)
        if wrapper is not None:
            missing = _missing_or_unchecked(
                os.path.realpath(command), f"its supervisor's command ({command})"
            )
            if missing is not None:
                return missing
            command, args = wrapper
        command, args, env = _unwrap_env(command, args, env)
    except _Inconclusive as exc:
        return ReentryVerdict(Reentry.INCONCLUSIVE, str(exc))
    except OSError as exc:
        return ReentryVerdict(
            Reentry.INCONCLUSIVE, f"its service definition could not be read ({exc})"
        )
    except subprocess.TimeoutExpired:
        return ReentryVerdict(Reentry.INCONCLUSIVE, "the service manager did not answer")
    verdict, _exec_failed = _command_check(command, env, "its supervisor's command", args=args)
    if verdict.status is not Reentry.REENTERABLE:
        return verdict
    from kiro_crew.service import live_target

    target, _ignored = live_target.read_target_reason()
    if target is None:
        return verdict
    hop, exec_failed = _command_check(str(live_target.target_bin(target)), env, "its live target")
    if exec_failed:
        # ``live_target.maybe_reexec`` catches an exec that cannot start and boots
        # the supervisor's own build instead, which the verdict above covers.
        return verdict
    return hop


def _supervisor_launch() -> tuple[str, list[str] | None, dict[str, str]]:
    """What the service manager executes: argv[0], the rest, and its environment.

    The rest is ``None`` when it is not known (a launchd launcher's target).
    """
    from kiro_crew.service import common, controller, macos

    if common.current_platform() == common.Platform.SYSTEMD:
        return _systemd_launch()
    path = controller.installed_unit_path()
    if path is None or path.suffix != ".plist":
        raise _Inconclusive("no installed service definition was found")
    payload = macos._plist_payload(path)
    if payload is None:
        raise _Inconclusive("the launchd agent's definition could not be read")
    argv = payload.get("ProgramArguments")
    if not isinstance(argv, list) or not argv:
        raise _Inconclusive("the launchd agent names no program")
    declared = payload.get("EnvironmentVariables")
    env = dict(os.environ)
    if isinstance(declared, dict):
        env.update({str(k): str(v) for k, v in declared.items()})
    return str(argv[0]), [str(word) for word in argv[1:]], env


def _wrapper_target(command: str, args: list[str] | None) -> tuple[str, list[str] | None] | None:
    """What a wrapper the supervisor execs leads to, or ``None`` for no wrapper.

    The launchd launcher is followed to the program it finally ``exec``s; an
    ``env`` command stays as it is, for :func:`_unwrap_env` to follow.
    """
    from kiro_crew.service import common, macos

    if common.current_platform() != common.Platform.SYSTEMD and command == str(macos.LIVE_PROGRAM):
        return _launcher_target(Path(command)), None
    if args is not None and os.path.basename(command) == "env":
        return command, args
    return None


def _unwrap_env(
    command: str, args: list[str] | None, env: dict[str, str]
) -> tuple[str, list[str] | None, dict[str, str]]:
    """Follow ``env [NAME=VALUE ...] /abs/program ...`` to the program it runs.

    ``env`` is a native binary that always starts, so judging it would pass a
    relaunch whose real program is gone. Its assignments join the environment;
    an option or a ``PATH`` lookup is not followed.
    """
    if args is None or os.path.basename(command) != "env":
        return command, args, env
    env = dict(env)
    rest = list(args)
    while rest and "=" in rest[0] and not rest[0].startswith("-"):
        key, _sep, value = rest.pop(0).partition("=")
        env[key] = value
    if not rest:
        raise _Inconclusive(f"its command ({command}) names no program")
    if rest[0].startswith("-"):
        raise _Inconclusive(
            f"its command ({command}) passes env an option this check cannot follow"
        )
    if not os.path.isabs(rest[0]):
        raise _Inconclusive(f"its command ({command}) looks {rest[0]} up on PATH")
    return rest[0], rest[1:], env


def _systemd_launch() -> tuple[str, list[str], dict[str, str]]:
    """The loaded unit that runs THIS process, asked of systemd itself.

    ``systemctl show`` answers with drop-ins merged, specifiers expanded and a
    reset ``ExecStart=`` applied, from whichever scope's unit has this process
    (or the launcher that spawned it) as its main pid. A unit whose file changed
    since it was loaded is not judged: the relaunch could run either version.
    """
    from kiro_crew.service import common, linux

    mine = {str(os.getpid()), str(os.getppid())}
    for user in (False, True):
        if user and linux._user_scope_unreachable_reason() is not None:
            continue
        props = _systemd_show(user)
        if props.get("LoadState") == ["loaded"] and props.get("MainPID", [""])[0] in mine:
            break
    else:
        raise _Inconclusive(f"no loaded {common.SERVICE_NAME}.service runs this gateway")
    if props.get("NeedDaemonReload") == ["yes"]:
        raise _Inconclusive("its unit changed on disk since systemd loaded it")
    match = _EXEC_START_PATH.search(" ".join(props.get("ExecStart", [])))
    if match is None:
        raise _Inconclusive("systemd reports no ExecStart for its unit")
    # systemctl joins argv[] with single spaces, unquoted; argv[0] is the path.
    args = match.group(2).split(" ")[1:]
    env = dict(os.environ)
    for assignment in shlex.split(" ".join(props.get("Environment", []))):
        key, sep, value = assignment.partition("=")
        if sep:
            env[key] = value
    # systemd applies EnvironmentFile= after Environment=, so it wins.
    for listed in props.get("EnvironmentFiles", []):
        for found in _ENVIRONMENT_FILE.finditer(listed):
            env.update(_environment_file(Path(found.group(1)), optional=found.group(2) == "yes"))
    return match.group(1), args, env


def _systemd_show(user: bool) -> dict[str, list[str]]:
    """The unit's properties as one scope's manager has them loaded.

    ``systemctl`` comes from the fixed system directories only: this runs inside
    the gateway, whose ``PATH`` can lead with a directory a planted shim could
    sit in.
    """
    from kiro_crew.service import common

    systemctl = platform_compat.trusted_system_bin("systemctl")
    if systemctl is None:
        raise _Inconclusive("systemctl is not in a trusted system directory")
    argv = [systemctl]
    if user:
        argv.append("--user")
    argv.append("show")
    for prop in (
        "LoadState",
        "MainPID",
        "NeedDaemonReload",
        "ExecStart",
        "Environment",
        "EnvironmentFiles",
    ):
        argv += ["-p", prop]
    argv.append(f"{common.SERVICE_NAME}.service")
    res = subprocess.run(
        argv,
        capture_output=True,
        check=False,
        env=common.systemctl_user_env() if user else None,
        timeout=_SYSTEMCTL_TIMEOUT_SECS,
        **UTF8_TEXT,
    )
    props: dict[str, list[str]] = {}
    for line in (res.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props.setdefault(key.strip(), []).append(value.strip())
    return props


def _environment_file(path: Path, *, optional: bool) -> dict[str, str]:
    """``KEY=VALUE`` lines of a systemd ``EnvironmentFile``; comments skipped."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if optional:
            return {}
        raise _Inconclusive(f"its EnvironmentFile {path} is missing") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise _Inconclusive(f"its EnvironmentFile {path} could not be read ({exc})") from exc
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            try:
                value = "".join(shlex.split(value))
            except ValueError:
                pass
        env[key.strip()] = value
    return env


def _launcher_target(launcher: Path) -> str:
    """What the launchd launcher script finally ``exec``s; the launcher if unparsable."""
    if not launcher.is_file():
        return str(launcher)
    try:
        text = launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Inconclusive(f"its launcher {launcher} could not be read ({exc})") from exc
    for line in reversed(text.splitlines()):
        if line.startswith("exec "):
            words = shlex.split(line, posix=True)
            return words[1] if len(words) > 1 else str(launcher)
    return str(launcher)


def _command_check(
    command: str, env: dict[str, str], what: str, *, args: list[str] | None = None
) -> tuple[ReentryVerdict, bool]:
    """Whether *command* would start the gateway when the service manager runs it.

    *args* are the words after *command*, when known. A native binary is judged
    only as the gateway itself (``<binary> gateway ...``): any other binary is a
    wrapper whose real program is in its arguments.

    The flag is ``True`` for a refusal the exec itself would raise (a command or
    interpreter that is missing, unreachable or not executable, or that the
    kernel will not run), as opposed to a process that starts and then fails.
    """
    from kiro_crew.apps.bridges import _python_shebang_interpreter

    if not command:
        return ReentryVerdict(Reentry.REFUSED, f"{what} is empty"), True
    target = os.path.realpath(command)
    missing = _missing_or_unchecked(target, f"{what} ({command})")
    if missing is not None:
        return missing, missing.status is Reentry.REFUSED
    try:
        with open(target, "rb") as handle:
            head = handle.read(4)
        is_script = head[:2] == b"#!"
    except OSError as exc:
        # Executable but unreadable: a script's interpreter cannot read it either.
        status = Reentry.REFUSED if exc.errno in _UNREACHABLE_ERRNOS else Reentry.INCONCLUSIVE
        return ReentryVerdict(status, f"{what} ({command}) could not be read ({exc})"), False
    if not is_script and not head.startswith(_NATIVE_MAGICS):
        # Neither a script nor an image the kernel runs (a console script
        # truncated mid-write lost its shebang): its exec fails with ENOEXEC.
        return (
            ReentryVerdict(
                Reentry.REFUSED, f"{what} ({command}) is not a script or a binary the kernel runs"
            ),
            True,
        )
    if not is_script:
        if args is not None and args[:1] != ["gateway"]:
            return (
                ReentryVerdict(
                    Reentry.INCONCLUSIVE,
                    f"{what} ({command}) is a wrapper this check cannot follow",
                ),
                False,
            )
        # Not probed: an executable image proves no dependency was synced.
        return ReentryVerdict(Reentry.REENTERABLE), False
    # The same sniff the app launcher uses: one absolute interpreter, no arguments.
    interpreter = _python_shebang_interpreter(target)
    if interpreter is None or "python" not in os.path.basename(interpreter).lower():
        # A shell wrapper (pip's own /bin/sh trampoline included), an env lookup
        # or a shebang with arguments: no fixed Python this can run the same way.
        return (
            ReentryVerdict(
                Reentry.INCONCLUSIVE, f"{what} ({command}) is a wrapper this check cannot follow"
            ),
            False,
        )
    missing = _missing_or_unchecked(interpreter, f"{what} ({command}) runs {interpreter}, which")
    if missing is not None:
        return missing, missing.status is Reentry.REFUSED
    try:
        imports = dep_sync.imports_gateway_entry_point(
            Path(interpreter), env=env, timeout=_IMPORT_PROBE_TIMEOUT_SECS
        )
    except subprocess.TimeoutExpired:
        return ReentryVerdict(Reentry.INCONCLUSIVE, "the import probe did not answer"), False
    except OSError as exc:
        fails = exc.errno in _EXEC_FAILS_ERRNOS
        status = Reentry.REFUSED if fails else Reentry.INCONCLUSIVE
        return ReentryVerdict(status, f"{what} ({command}) runs {interpreter} ({exc})"), fails
    if imports:
        return ReentryVerdict(Reentry.REENTERABLE, probed=True), False
    return (
        ReentryVerdict(
            Reentry.REFUSED,
            f"{what} ({command}) runs {interpreter}, which cannot import the gateway; "
            f"repair it with: {_import_remedy(interpreter)}",
        ),
        False,
    )


def _missing_or_unchecked(path: str, what: str) -> ReentryVerdict | None:
    """REFUSED when *path* is not a file, INCONCLUSIVE when it cannot be checked."""
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        if exc.errno in _MISSING_ERRNOS:
            return ReentryVerdict(Reentry.REFUSED, f"{what} is missing")
        if exc.errno in _UNREACHABLE_ERRNOS:
            # The service runs as this user: its exec cannot reach it either.
            return ReentryVerdict(Reentry.REFUSED, f"{what} cannot be reached ({exc})")
        return ReentryVerdict(Reentry.INCONCLUSIVE, f"{what} could not be checked ({exc})")
    if not stat.S_ISREG(mode):
        return ReentryVerdict(Reentry.REFUSED, f"{what} is not a file")
    if not os.access(path, os.X_OK):
        return ReentryVerdict(Reentry.REFUSED, f"{what} is not executable")
    return None


def _import_remedy(interpreter: str) -> str:
    """``pip install -e`` only for this checkout's own pip-managed venv; else the installer.

    The same pairing the gateway's console-script repair uses: an interpreter
    that is not the project's venv belongs to another install, and an editable
    install of this checkout there would replace it.
    """
    proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
    if proj:
        try:
            method = (Path(proj) / ".install-method").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            method = ""
        venv_py = dep_sync.project_venv_python(Path(proj))
        if method == "pip" and os.path.abspath(interpreter) == os.path.abspath(venv_py):
            return f"{shlex.quote(interpreter)} -m pip install -e {shlex.quote(proj)}"
    return "re-run the installer"

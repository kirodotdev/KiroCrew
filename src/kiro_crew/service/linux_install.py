"""``kirocrew service install`` on Linux, and the AppArmor profiles it attaches.

Writes the unit and the operator-editable overrides file as root, refuses a host
where SELinux proves the system unit cannot start (:func:`selinux_refusal`, whose
remedy embeds a rendered user unit), enables and starts the service
(:func:`install`), and installs or removes the userns AppArmor profiles for the
service and for a directly launched app. Imports
:mod:`kiro_crew.service.linux_unit` and :mod:`kiro_crew.service.linux_systemctl`.

Part of the :mod:`kiro_crew.service.linux` family: callers and tests reach every name
through that facade, which keeps the module's patch seams whole
(:mod:`kiro_crew._facade_mirror`). In the docstrings and comments below, "this module"
means that family and "the module docstring" means the facade's, which carries the
systemd 219 commitment and the sudo scope.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

from kiro_crew.service import apparmor, selinux
from kiro_crew.service.common import SERVICE_NAME, kirocrew_bin
from kiro_crew.service.linux_systemctl import (
    ServiceInstallError,
    _privilege_prefix,
    _require_privilege,
    _sudo_run,
    _systemctl,
)
from kiro_crew.service.linux_unit import (
    _ENV_FILE_TEMPLATE,
    ENV_DIR,
    ENV_FILE_PATH,
    UNIT_PATH,
    USER_UNIT_SUBDIR,
    _current_uid,
    _current_user,
    _home_for_user,
    render_unit,
)

# The family logs under the facade's name, the one log filters and caplog read.
log = logging.getLogger("kiro_crew.service.linux")


def _write_unit_via_sudo(contents: str) -> subprocess.CompletedProcess[str]:
    """Write the unit file at ``UNIT_PATH`` atomically via ``sudo install``.

    Writes contents to a user-owned temp file first, then uses
    ``sudo install -m 0644 -o root -g root`` to atomically place it at
    ``UNIT_PATH`` with the correct ownership and mode in a single step.
    The atomic rename inside ``install`` means a SIGINT or crash mid-write
    leaves either the old unit file (if any) or no file at all — never a
    partially-written file that systemd would fail to parse on
    ``daemon-reload``.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="kirocrew-unit-", suffix=".service")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        return subprocess.run(
            [
                *_privilege_prefix(),
                "install",
                "-m",
                "0644",
                "-o",
                "root",
                "-g",
                "root",
                tmp_path,
                str(UNIT_PATH),
            ],
            capture_output=True,
            text=True,  # subprocess-encoding: locale -- sudo install writes in the locale
            check=False,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _install_file_via_sudo(contents: str, dest: Path, mode: str = "0644") -> None:
    """Atomically place ``contents`` at ``dest`` as root, like the unit write.

    Same escalation path as the unit file — no second mechanism, and no kirocrew
    or LLM-influenced code runs under sudo; only ``install`` is invoked.
    """
    fd, tmp_path = tempfile.mkstemp(prefix="kirocrew-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(contents)
        res = _sudo_run("install", "-m", mode, "-o", "root", "-g", "root", tmp_path, str(dest))
        if res.returncode != 0:
            raise ServiceInstallError((res.stderr or res.stdout).strip())
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _sudo_capture(*argv: str) -> tuple[int, str]:
    """Run one privileged command, returning ``(rc, combined output)``.

    Needed for the AppArmor enforcement check: it must run under sudo (an
    unconfined user cannot aa_change_onexec into a named profile, and aa-exec
    does not fail loudly when it cannot transition) AND its exit code is the
    answer rather than an error, so it cannot use the raising helper.

    The check runs a trusted ``python3`` as root on the ``sudo kirocrew service
    install`` path, so it starts from the fixed root-owned directory
    ``/`` rather than the operator's working directory, and with every
    ``PYTHON*`` variable removed from the environment it inherits. The probe
    also passes ``-I``; these two keep the same guarantee should any other
    child in the chain read the working directory or those variables.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    res = _sudo_run(*argv, cwd=_PROBE_CWD, env=env)
    return (res.returncode, (res.stderr or "") + (res.stdout or ""))


# Where the privileged enforcement probe starts. Root-owned and not writable by
# an ordinary user, unlike whatever directory the operator ran the install from.
_PROBE_CWD = "/"


def _sudo_run_checked(*argv: str) -> None:
    """Run one privileged command, raising on a non-zero exit."""
    res = _sudo_run(*argv)
    if res.returncode != 0:
        raise ServiceInstallError((res.stderr or res.stdout).strip())


def _seed_env_file() -> None:
    """Create the operator-editable overrides file if it does not already exist.

    Create-if-absent is the whole contract: a re-install must never clobber an
    operator's edits (the value they set here is precisely what survives a
    re-install, unlike the baked ``Environment=`` snapshot). Best-effort — the
    unit references it with ``EnvironmentFile=-`` so a seeding failure is
    non-fatal and simply leaves the baked defaults in force, and install() must
    proceed to daemon-reload/restart regardless.

    Existence is probed through a PRIVILEGED ``test -e``, never
    ``Path.exists()``: a pre-existing root-owned ``/etc/kirocrew`` with a
    restrictive mode makes ``Path.exists()`` raise ``PermissionError`` under the
    invoking (non-root) user on Python 3.12, which would abort the install. The
    whole body is wrapped so any unexpected error degrades to a warning.
    """
    try:
        # rc 0 => the file already exists (privileged stat sees through a
        # root-only directory); leave whatever is there untouched.
        if _sudo_run("test", "-e", str(ENV_FILE_PATH)).returncode == 0:
            return
        _sudo_run("mkdir", "-p", str(ENV_DIR))
        # 0644: readable so an operator can inspect it, root-owned so an
        # unprivileged process cannot rewrite the service's environment.
        _install_file_via_sudo(_ENV_FILE_TEMPLATE, ENV_FILE_PATH, mode="0644")
    except (ServiceInstallError, OSError):
        log.warning("Could not seed %s; the service uses baked defaults", ENV_FILE_PATH)


def _env_file_is_untouched_seed() -> bool:
    """True only when the overrides file still holds our exact seed template.

    Uninstall uses this to decide whether the file is ours to delete. Any
    difference — an operator edit, a file they pre-provisioned before install,
    or an unreadable file — returns False so their configuration is left intact.
    """
    try:
        return ENV_FILE_PATH.read_text(encoding="utf-8") == _ENV_FILE_TEMPLATE
    except (OSError, UnicodeDecodeError):
        # Not UTF-8 means an operator wrote it (our seed is ASCII): theirs to keep.
        return False


def _user_scope_remedy() -> str:
    """The commands that stand up a working per-user unit on this host.

    Shared by :func:`selinux_refusal` (pre-flight proved the system unit cannot
    start) and :func:`selinux_start_failure_hint` (it started nothing and SELinux
    is enforcing), so the operator is handed the same verified sequence either
    way and the two cannot drift.

    **Every path and account is spelled out, and none is taken from the pasting
    shell.** A user unit has no ``User=`` — the account it runs as is whichever
    manager loads it — so ``~`` and ``$USER`` would decide who runs the agent.
    ``service install`` is documented to run under ``sudo``, so the shell reading
    this is usually root's: a tilde would name ``/root``, ``$USER`` would expand to
    ``root``, and the remedy would hand an operator a unit that runs untrusted
    agent tools as root — defeating the same invariant :func:`install` enforces by
    refusing a ``User=root`` unit. Hence the absolute home, the explicit account
    name, and the warning.

    Both generated paths go through :func:`shlex.quote`, like the ``.env`` remedy
    in :mod:`kiro_crew.service.common`: these lines are copy-pasted verbatim, and
    an account home containing a space would word-split, so ``mkdir`` would create
    the wrong directories and the redirect would put the unit somewhere systemd
    never reads. Ordinary paths come back unquoted, so the common case is
    unchanged.
    """
    unit_body = render_unit(user_scope=True)
    user = _current_user()
    home = _home_for_user(user) if user else str(Path.home())
    account = user or "<the service account>"
    unit_dir = shlex.quote(str(Path(home) / USER_UNIT_SUBDIR))
    unit_file = shlex.quote(str(Path(home) / USER_UNIT_SUBDIR / UNIT_PATH.name))
    return (
        f"   Run the next four commands AS {account} — a user unit carries no\n"
        f"   User=, so it runs as whichever account's manager loads it. Loading it\n"
        f"   from a root shell (the shell you are probably in, since `service\n"
        f"   install` needs sudo) would run the agent as ROOT, which this installer\n"
        f"   otherwise refuses outright. `sudo -u {account}` is NOT enough — it\n"
        f"   creates no session, so `systemctl --user` cannot reach that account's\n"
        f"   manager. Get a real session first, e.g. `machinectl shell {account}@`,\n"
        f"   or just log in as {account}.\n"
        f"\n"
        f"     mkdir -p {unit_dir}\n"
        f"     cat > {unit_file} <<'KIROCREW_UNIT'\n"
        f"{unit_body}"
        f"KIROCREW_UNIT\n"
        f"     systemctl --user daemon-reload\n"
        f"     systemctl --user enable --now {SERVICE_NAME}.service\n"
        f"\n"
        f"   Then, back in a root shell — this one step needs privilege, and takes\n"
        f"   the account name explicitly so it cannot land on the wrong user:\n"
        f"\n"
        f"     loginctl enable-linger {shlex.quote(account)}\n"
        f"\n"
        f"   Manage it with `systemctl --user status|restart {SERVICE_NAME}` and\n"
        f"   `journalctl --user -u {SERVICE_NAME} -f`. `kirocrew service "
        f"status|uninstall`\n"
        f"   report and remove it as the user scope — run them as {account}, not\n"
        f"   under sudo, since a root shell cannot reach that account's manager."
    )


def selinux_refusal(reason: str) -> str:
    """Operator-facing refusal for a system unit SELinux proves cannot start.

    A refusal rather than a warning because everything after this point is
    destructive to no purpose: install would write the unit, ``enable`` it, fail
    at the first ``systemctl restart``, and leave a unit enabled that crash-loops
    at every boot until it exhausts ``StartLimitBurst``. Stopping before the
    first write leaves the host exactly as it was found.

    The remedy embeds a ready-to-paste user unit rendered by :func:`render_unit`,
    not prose describing one: the operator's working unit then carries the same
    ``ExecStart`` and the same baked environment as the unit we would have
    installed, and cannot drift from it as this module changes.
    """
    return (
        f"Refusing to install a system service that cannot start on this host.\n"
        f"   {reason}.\n"
        f"\n"
        f"   This is SELinux type enforcement, not a broken file. The binary is\n"
        f"   perfectly ordinary — it exists, it is executable, and `test -x` on\n"
        f"   it succeeds; the policy's execute check is the only thing that\n"
        f"   fails, and nothing short of asking the policy reveals it. A unit at\n"
        f"   {UNIT_PATH} would fail every start with\n"
        f"   status=203/EXEC until it hit its restart limit.\n"
        f"\n"
        f"   A per-user unit is not subject to this: the per-user systemd manager\n"
        f"   does not run in PID 1's domain, so it is allowed to execute a binary\n"
        f"   under $HOME.\n"
        f"\n"
        f"{_user_scope_remedy()}\n"
        f"\n"
        f"   Installing kirocrew outside $HOME (onto a system-labelled path such\n"
        f"   as /usr/local/bin) also resolves it. Relocating only the LAUNCHER\n"
        f"   does not: whatever systemd execs still runs in PID 1's domain, so\n"
        f"   the next execve of the binary under $HOME is denied identically."
    )


def selinux_start_failure_hint() -> str:
    """SELinux context to append when the unit was written but would not start.

    Covers the residue the pre-flight cannot prove. That check asks only whether
    PID 1's domain may execute the file systemd itself ``execve``s; it cannot
    follow what that file execs at runtime, so a ``KIROCREW_SERVICE_BIN`` override
    naming a system-labelled wrapper that later runs a binary under ``$HOME``
    passes the gate and still fails — as the shell's exit 126 rather than
    ``203/EXEC``, since the wrapper itself execs fine. Rather than guess at a
    wrapper's contents (see the boundary discussion in
    :mod:`kiro_crew.service.selinux`), name SELinux here, where the unit has
    actually failed, so the operator is never left with only "run journalctl".

    Deliberately the HYPOTHESIS and the command that settles it — NOT the
    user-scope remedy :func:`selinux_refusal` prints. This fires on every failed
    restart on every enforcing host, which is all of RHEL/Fedora, including the
    ones the pre-flight positively proved ALLOW for; a port conflict on a stock
    RHEL box would otherwise be answered with a wall of SELinux text and a
    pasteable unit for a denial nobody has observed. A remedy belongs behind a
    proven denial, so this points at the documented one and stops.

    Empty on any host that is not enforcing, so nothing changes on the
    overwhelming majority of installs.
    """
    if not selinux.is_enforcing():
        return ""
    return (
        f"\n\nSELinux is enforcing here, which is one common cause of a unit that\n"
        f"   installs and then will not start. This is a hypothesis, not a finding:\n"
        f"   the pre-flight found no proven denial for {kirocrew_bin()}, but it only\n"
        f"   checks the file systemd execs and the interpreter its shebang names, so\n"
        f"   if that file is a wrapper, whatever IT runs is not covered. Settle it\n"
        f"   with:\n"
        f"\n"
        f"     sudo ausearch -m avc -ts recent\n"
        f"\n"
        f"   An `avc: denied {{ execute }}` naming the gateway binary means no system\n"
        f"   unit can work on this host. The per-user remedy is in\n"
        f'   docs/guides/install.md, "SELinux-enforcing hosts with kirocrew under\n'
        f'   $HOME". No such denial means this failure is something else.'
    )


def install() -> apparmor.ProfileOutcome:
    """Write the unit file and enable+start the service. Idempotent.

    Calls ``sudo`` to write the unit and to invoke ``systemctl``. Sudo
    will prompt for a password the first time (or when the cached
    ticket has expired) — that prompt appears on the user's terminal.
    No kirocrew / LLM / agent code runs under sudo deliberately — see the module
    docstring's sudo scope for the full set of escalated programs, including
    the ones the AppArmor step adds, and for what the escalated interpreter can
    still load on its own.

    Raises :class:`ServiceInstallError` with a human-readable message if
    a step fails. The CLI catches this and prints the message instead
    of letting a CalledProcessError surface.

    Returns the AppArmor profile outcome for the caller to report. The profile is
    installed BEFORE systemd starts the unit: the unit carries no
    ``AppArmorProfile=`` line, and the path-attached profile is applied by the
    kernel at ``execve()`` of the launcher, so loading it afterwards would leave
    the first gateway process unprofiled and every agent spawn failing closed
    until the next restart.
    """
    # Fail early and cleanly if we cannot escalate: without this the first
    # `sudo` call raises FileNotFoundError, which controller.install_service
    # does not catch, so the CLI prints a traceback instead of the reason.
    _require_privilege()

    user = _current_user()
    if not user:
        raise ServiceInstallError(
            "Could not determine current user (USER and LOGNAME both unset). "
            "Set $USER and re-run."
        )
    # Never render a root-run agent. The gateway imports MCP / LLM / agent code,
    # and the module invariant is that it runs as the invoking human, never
    # root. When invoked as bare root (a root login, or `sudo` with no
    # SUDO_USER) `user` resolves to "root"; refuse rather than write a
    # `User=root` unit that would run untrusted tools with host-wide root. The
    # operator picks a real account via `sudo -u <user>` / `SUDO_USER` / `$USER`.
    if user == "root":
        raise ServiceInstallError(
            "Refusing to install a service that runs the agent as root. The "
            "gateway runs untrusted tools and must run as a normal user. Re-run "
            "as that user (e.g. via their login, or `sudo -u <user> kirocrew "
            "service install`), or set $USER to a non-root account."
        )

    # Last gate before anything is written: on an SELinux-enforcing host whose
    # kirocrew lives under $HOME, PID 1's domain is denied execute on the binary
    # this unit would name, so the unit can never start. Everything below
    # would still "succeed" up to the first `systemctl restart`, leaving an
    # enabled unit crash-looping at 203/EXEC on every boot. Fires only on a
    # proven policy denial and fails open on every indeterminate answer, so a
    # host without SELinux, or in permissive mode, is unaffected.
    blocked, selinux_reason = selinux.blocks_system_unit(kirocrew_bin())
    if blocked:
        raise ServiceInstallError(selinux_refusal(selinux_reason))

    needs_profile, profile_reason = apparmor.should_install()
    write_res = _write_unit_via_sudo(render_unit())
    if write_res.returncode != 0:
        raise ServiceInstallError(
            "Failed to write the unit file. The sudo step is required because "
            f"{UNIT_PATH} is owned by root.\n"
            f"   sudo install said: {(write_res.stderr or write_res.stdout).strip()}"
        )

    # Seed the operator-editable overrides file (create-if-absent), so a later
    # `KIROCREW_PORT=...` edit + restart works without re-installing.
    _seed_env_file()

    # Before daemon-reload/enable/restart: a path-attached profile applies at the
    # kernel's own execve() time, so it must already be loaded or the first
    # gateway process (and everything it forks) comes up unprofiled.
    profile_outcome = (
        install_apparmor_profile(_current_uid(user))
        if needs_profile
        else apparmor.ProfileOutcome(False, f"AppArmor profile not needed: {profile_reason}")
    )

    reload_res = _systemctl("daemon-reload")
    if reload_res.returncode != 0:
        raise ServiceInstallError(
            f"`sudo systemctl daemon-reload` failed: "
            f"{(reload_res.stderr or reload_res.stdout).strip()}"
        )

    enable_res = _systemctl("enable", f"{SERVICE_NAME}.service")
    if enable_res.returncode != 0:
        raise ServiceInstallError(
            f"`sudo systemctl enable` failed: "
            f"{(enable_res.stderr or enable_res.stdout).strip()}"
        )

    # Use restart (not start) so re-running install picks up a unit-file
    # change without manual intervention.
    restart_res = _systemctl("restart", f"{SERVICE_NAME}.service")
    if restart_res.returncode != 0:
        raise ServiceInstallError(
            f"`sudo systemctl restart` failed: "
            f"{(restart_res.stderr or restart_res.stdout).strip()}\n"
            f"Run `sudo journalctl -u {SERVICE_NAME}.service -n 50` for details."
            # The pre-flight only proves denials for the file systemd itself
            # execs, so a wrapper's delegated binary can still be denied and land
            # here. Name SELinux where the unit has actually failed rather than
            # leave the operator with only a journalctl command.
            + selinux_start_failure_hint()
        )

    return profile_outcome


def install_apparmor_profile(expected_uid: int | None) -> apparmor.ProfileOutcome:
    """Install the userns AppArmor profile when this host needs one.

    Deliberately NOT fatal: a gateway running without the profile is the status
    quo, whereas aborting a service install because a hardening step failed is a
    regression. The caller prints the outcome and continues either way.

    Attaches the profile to ``kirocrew_bin()`` — the same resolved path
    ``render_unit()`` uses for ``ExecStart`` — instead of relying on
    ``AppArmorProfile=`` (see the module docstring in ``apparmor.py``).

    ``expected_uid`` is the numeric uid of the account the SERVICE runs as
    (``_current_uid(_current_user())``, resolved once by the caller): the
    installer process itself may be running as root (bare root, or under
    ``sudo``), but the launcher script being attached is expected to be owned by
    the human the gateway's ``User=`` names, not by whichever account happens to
    be executing this installer. When that account cannot be resolved
    (``expected_uid is None``) the install is SKIPPED rather than attempted:
    :func:`apparmor._substitutable_by_others` reads ``None`` as "check against
    the calling process's own uid" — the AppImage semantics — and under ``sudo``
    that calling uid is root, so a root-owned, host-shared launcher would pass
    the ownership check and the path-keyed userns grant would extend to every
    account that runs it. Skipping mirrors how an unresolvable ``exec_path`` is
    already a named non-fatal skip, and re-running the install once the account
    resolves is the documented recovery.
    """
    if expected_uid is None:
        return apparmor.ProfileOutcome(
            False,
            "AppArmor profile not installed: the service account's uid could not "
            "be resolved, and without it the ownership check that keeps this "
            "path-keyed userns grant scoped to that account cannot run — the "
            "fallback would be the installer's own uid (root, under `sudo "
            "kirocrew service install`), which would accept a root-owned shared "
            "launcher and hand the grant to every account on this host. The "
            "service was installed without the profile; re-run `kirocrew "
            "service install` once the account resolves.",
            ok=False,
        )
    # uid/gid, not sys.executable: the verification drops privilege back to the
    # invoking user inside the profile and runs a TRUSTED system python, because
    # the venv interpreter is user-writable and must never execute under sudo.
    return apparmor.install(
        _install_file_via_sudo,
        _sudo_run_checked,
        _sudo_capture,
        os.getuid(),
        os.getgid(),
        exec_path=kirocrew_bin(),
        expected_uid=expected_uid,
    )


def remove_apparmor_profile() -> apparmor.ProfileOutcome:
    """Unload and delete the profile so uninstall leaves the host as it was."""
    return apparmor.uninstall(_sudo_run_checked)


def install_launcher_profile(exec_path: str | None = None) -> apparmor.ProfileOutcome:
    """Attach the userns profile to a directly launched app (AppImage/desktop).

    Same three privileged helpers as the service path — one escalation mechanism
    for both profiles, and still nothing but ``install`` / ``apparmor_parser`` /
    ``aa-exec`` running under sudo. No kirocrew or LLM-influenced code does.

    Unlike the service profile this is NOT reached from ``service install``: a
    direct launch has no unit to hang it off, so the user (or the desktop app,
    which surfaces the exact command) invokes it explicitly.
    """
    return apparmor.install_launcher(
        _install_file_via_sudo,
        _sudo_run_checked,
        _sudo_capture,
        os.getuid(),
        os.getgid(),
        exec_path,
    )


def remove_launcher_profile() -> apparmor.ProfileOutcome:
    """Unload and delete the launcher profile, leaving the host as it was found."""
    return apparmor.uninstall_launcher(_sudo_run_checked)

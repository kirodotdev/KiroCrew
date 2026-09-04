"""systemd ``--user`` template unit for pods — generated, not shipped.

The unit's ``ExecStart`` re-enters a ``kirocrew`` binary as ``kirocrew pod _run
%i``, so the boot logic lives in Python (see :func:`kiro_crew.pod.runtime.boot`)
and nothing is shipped outside the package. The template can only name ONE
binary for every instance, so each pod additionally gets a per-instance drop-in
(:func:`install_dropin`) pinning ``ExecStart`` to its OWN checkout's ``kirocrew``
— without it a pod runs whichever build ``pod install`` resolved rather than the
worktree it exists to test.

The unit deliberately has **no ``ExecStopPost`` teardown hook**. systemd runs
``ExecStopPost`` before the final kill of the unit's cgroup, so a hook that
deleted the pod's isolated HOME raced the pod's own surviving subprocesses (which
recreated the directory by reopening their audit log in append mode), and it also
fired on the stop half of a ``Restart=``, restarting the pod onto a home that no
longer held its sessions or config. Reclamation therefore belongs to
:func:`kiro_crew.pod.runtime.stop_pod`, which runs it after the service is
confirmed down and its process tree has drained; ``pod ls`` reports the HOMEs left
by a pod that went away without a ``down``.
"""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.atomic_write import atomic_write_at
from kiro_crew.pod import provision as prov
from kiro_crew.pod.config import PodConfig, environment_vars
from kiro_crew.service.common import systemd_quote

_UNIT_TEMPLATE = """\
[Unit]
Description=KiroCrew pod (%i) — isolated worktree gateway, throwaway
# Multi-active: many run at once, one per worktree, each on its own port + own
# KIROCREW_HOME. Orthogonal to the live gateway; refuses to bind the live port.

[Service]
Type=simple
# Pin the pod plane into the unit so the gateway booted by systemd resolves the
# SAME PodConfig as the CLI that installed it (systemd starts with a clean env).
# Only non-default values are emitted; an empty block means all-defaults.
{environment}# Boot the isolated instance. %i = worktree name. Re-enters the installed
# kirocrew binary; boot logic is kiro_crew.pod.runtime.boot (no shipped shell).
ExecStart={kirocrew_bin} pod _run %i

# NO teardown hook here: systemd runs ExecStopPost before the final kill of this
# cgroup, so one would delete the pod's HOME out from under the pod's own
# surviving subprocesses — and would also fire on the stop half of a Restart=,
# bringing the pod back on a wiped home. `kirocrew pod down` owns reclamation
# (kiro_crew.pod.runtime.stop_pod); `kirocrew pod ls` reports what a crash left.

# Self-heal a crash, but don't fight a deliberate stop.
Restart=on-failure
RestartSec=5

# Resource isolation: cap so a runaway pod can't starve the live plane.
MemoryMax=4G
CPUQuota=200%

# journalctl --user -u {unit_prefix}@<name>
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _kirocrew_argv() -> tuple[str, ...]:
    """Argv prefix that re-enters the kirocrew entry point the unit should boot.

    Resolution order:
      1. ``KIROCREW_POD_BIN`` — explicit override (used when installing a unit that
         must boot a specific build, e.g. a worktree's own ``.venv``).
      2. the console-script on PATH.
      3. ``<this python> -m kiro_crew`` so it works from a bare checkout.
    """
    override = os.environ.get("KIROCREW_POD_BIN")
    if override:
        return (override,)
    found = shutil.which("kirocrew")
    if found:
        return (found,)
    return (sys.executable, "-m", "kiro_crew")


def _environment_block(cfg: PodConfig) -> str:
    """``Environment=`` lines for the shared pod-plane env selection.

    The *selection* lives in :func:`kiro_crew.pod.config.environment_vars` so the
    launchd backend pins the identical plane; this function only serialises it in
    systemd's syntax. Returns "" when everything is at defaults.
    """
    return "".join(
        f"Environment={systemd_quote(f'{key}={val}')}\n"
        for key, val in environment_vars(cfg).items()
    )


def render_unit(cfg: PodConfig) -> str:
    return _UNIT_TEMPLATE.format(
        kirocrew_bin=" ".join(systemd_quote(arg) for arg in _kirocrew_argv()),
        unit_prefix=cfg.unit_prefix,
        environment=_environment_block(cfg),
    )


def unit_path(cfg: PodConfig) -> Path:
    """Where the template unit is installed for the current user."""
    return Path.home() / ".config" / "systemd" / "user" / f"{cfg.unit_prefix}@.service"


# --------------------------------------------------------------------------- #
# Per-instance drop-in — what makes a pod run ITS OWN worktree's code.
#
# The unit above is a TEMPLATE shared by every pod, so its ``ExecStart`` can only
# bake ONE kirocrew binary: whichever one ``pod install`` happened to resolve,
# normally the globally installed ``~/.local/bin/kirocrew``. Every pod therefore
# booted through that build regardless of which checkout it was pinned to, and a
# pod exists precisely to run a worktree's own code. The visible cost was silent:
# a global build predating a feature simply ignored the env-file key carrying it
# (``SEED=`` being the sharp one), so the pod came up healthy and unseeded while
# the worktree's own ``boot`` — which does honour it — never ran.
#
# systemd resolves ``<unit>.d/*.conf`` per INSTANCE, so one drop-in per pod pins
# that pod to its checkout without touching the shared template. The empty
# ``ExecStart=`` is required: for a ``Type=simple`` unit the directive is a list,
# and a second value would append a command rather than replace the template's.
# --------------------------------------------------------------------------- #

_DROPIN_FILENAME = "50-kirocrew-pod.conf"

_DROPIN_TEMPLATE = """\
# Written by `kirocrew pod up` — removed by `kirocrew pod down`. Do not edit.
[Service]
# Reset the template's ExecStart (a list directive, so an unreset second value
# would APPEND) and boot this pod through its own checkout's kirocrew. The pod
# then runs the worktree's code, which is the whole point of a pod: the template
# alone bakes one global binary for every instance.
ExecStart=
ExecStart={kirocrew_bin} pod _run %i
"""


def dropin_dir(cfg: PodConfig, name: str) -> Path:
    """Drop-in directory systemd reads for pod *name* alone."""
    return unit_path(cfg).with_name(f"{cfg.unit_prefix}@{name}.service.d")


def dropin_path(cfg: PodConfig, name: str) -> Path:
    """The uniquely owned drop-in file ``pod up`` writes for pod *name*.

    ``override.conf`` belongs to ``systemctl edit`` and therefore to the
    operator. Claiming that conventional name would overwrite their settings on
    every up and delete them on down.
    """
    return dropin_dir(cfg, name) / _DROPIN_FILENAME


def render_dropin(checkout: Path) -> str:
    """The drop-in pinning a pod's boot to *checkout*'s own ``kirocrew``."""
    return _DROPIN_TEMPLATE.format(kirocrew_bin=systemd_quote(str(prov.venv_bin(checkout))))


def _write_unit_file_atomic_nofollow(dst: Path, content: str, *, what: str) -> None:
    """Publish one managed systemd file without following planted links."""
    dir_fd = pinned_fs.create_and_open_dir_pinned(
        dst.parent,
        what=f"{what} directory",
        refusal=OSError,
    )
    try:
        try:
            existing = os.stat(dst.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(existing.st_mode):
                raise OSError(f"refusing to write {what} {dst}: it is a symbolic link")
            if not stat.S_ISREG(existing.st_mode):
                raise OSError(f"refusing to write {what} {dst}: it is not a regular file")
        atomic_write_at(dir_fd, dst.name, content, fsync=True, mode=0o600)
    finally:
        os.close(dir_fd)


def install_dropin(cfg: PodConfig, name: str, checkout: Path) -> Path:
    """Write pod *name*'s drop-in and return its path. Caller runs daemon-reload.

    Rewritten on every start rather than created once, so a pod re-``up``ped from
    a different checkout — or one whose venv was rebuilt elsewhere — cannot keep
    booting a path that no longer exists (the failure mode ``unit_exec_ok``
    exists to self-heal for the template).
    """
    dst = dropin_path(cfg, name)
    _write_unit_file_atomic_nofollow(
        dst,
        render_dropin(checkout),
        what="pod boot override",
    )
    return dst


def remove_dropin(cfg: PodConfig, name: str) -> bool:
    """Delete only Kiro Crew's drop-in. True when its file and empty dir are gone.

    A foreign drop-in keeps the directory alive. The POSIX path addresses the
    managed filename relative to a pinned directory descriptor, so a planted
    directory link cannot redirect cleanup into an operator-controlled target.
    """
    path = dropin_path(cfg, name)
    directory = dropin_dir(cfg, name)
    try:
        dir_fd = pinned_fs.open_dir_pinned(
            directory,
            what="pod boot override directory",
            refusal=OSError,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        try:
            os.unlink(path.name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
    except OSError:
        return False
    finally:
        os.close(dir_fd)

    try:
        parent_fd = pinned_fs.pin_parent(
            os.path.realpath(directory.parent),
            what="pod boot override directory",
            refusal=OSError,
        )
    except OSError:
        return False
    try:
        try:
            os.rmdir(directory.name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # A foreign drop-in makes the directory non-empty and therefore not
            # ours to remove. Every other failure means our empty per-pod
            # directory may remain, so teardown must not claim zero residue.
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                return False
    finally:
        os.close(parent_fd)
    return True


def install_unit(cfg: PodConfig) -> Path:
    """Write the template unit and return its path. Caller runs daemon-reload."""
    dst = unit_path(cfg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write_unit_file_atomic_nofollow(
        dst,
        render_unit(cfg),
        what="pod template unit",
    )
    return dst


def unit_exec_ok(cfg: PodConfig) -> bool:
    """True when the installed unit's baked ExecStart binary still exists.

    The unit bakes an absolute kirocrew path at install time (often a
    ``~/.local/bin`` symlink into some worktree's venv). Worktrees are
    ephemeral -- pruning the one the symlink resolves into leaves the unit
    permanently failing EXEC (status=203) until reinstall. Callers use this
    to self-heal by re-rendering the unit with a currently-valid binary
    before starting a pod.
    """
    dst = unit_path(cfg)
    try:
        text = dst.read_text()
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("ExecStart="):
            try:
                argv = shlex.split(line[len("ExecStart="):], posix=True)
            except ValueError:
                return False
            if not argv:
                return False
            # ``systemd_quote`` doubles percent signs to suppress specifier
            # expansion; recover the literal executable path before probing it.
            exe = argv[0].replace("%%", "%")
            return os.access(exe, os.X_OK) if os.path.isabs(exe) else True
    return False


# Directives an older installed unit may still carry that this build has removed.
# ``ExecStopPost`` runs teardown before systemd's final kill of the pod's cgroup,
# so it races the pod's own subprocesses and also wipes the HOME on the stop half
# of a ``Restart=``; reclamation belongs to the ``down`` path instead. Must stay a
# tuple: ``unit_is_current`` hands it straight to ``str.startswith``.
_REMOVED_DIRECTIVES: tuple[str, ...] = ("ExecStopPost=",)


def unit_is_current(cfg: PodConfig) -> bool:
    """True when the installed unit is one this build is willing to boot.

    Two ways it can be stale, and a start self-heals both by re-rendering: the
    baked ExecStart binary no longer exists (:func:`unit_exec_ok`), or the unit
    still carries a directive this build has removed. The second matters on
    UPGRADE — the unit is written once by ``pod install``, so without this check a
    machine that installed an older Kiro Crew would keep the teardown hook, and
    keep the defect, until someone happened to reinstall by hand.
    """
    if not unit_exec_ok(cfg):
        return False
    try:
        text = unit_path(cfg).read_text()
    except OSError:
        return False
    # str.startswith takes the whole tuple, so one pass answers for every removed
    # directive.
    return not any(line.startswith(_REMOVED_DIRECTIVES) for line in text.splitlines())

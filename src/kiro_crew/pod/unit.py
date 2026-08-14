"""systemd ``--user`` template unit for pods — generated, not shipped.

The unit's ``ExecStart`` re-enters the installed ``kirocrew`` binary as
``kirocrew pod _run %i``, so the boot logic lives in Python (see
:func:`kiro_crew.pod.runtime.boot`) and nothing is shipped outside the package.

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

import os
import shutil
import sys
from pathlib import Path

from kiro_crew.pod.config import PodConfig, environment_vars

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


def _kirocrew_bin() -> str:
    """Absolute path (or module invocation) to the kirocrew entry-point the unit
    should boot.

    Resolution order:
      1. ``KIROCREW_POD_BIN`` — explicit override (used when installing a unit that
         must boot a specific build, e.g. a worktree's own ``.venv``).
      2. the console-script on PATH.
      3. ``<this python> -m kiro_crew`` so it works from a bare checkout.
    """
    override = os.environ.get("KIROCREW_POD_BIN")
    if override:
        return override
    found = shutil.which("kirocrew")
    if found:
        return found
    return f"{sys.executable} -m kiro_crew"


def _environment_block(cfg: PodConfig) -> str:
    """``Environment=`` lines for the shared pod-plane env selection.

    The *selection* lives in :func:`kiro_crew.pod.config.environment_vars` so the
    launchd backend pins the identical plane; this function only serialises it in
    systemd's syntax. Returns "" when everything is at defaults.
    """
    return "".join(f"Environment={key}={val}\n" for key, val in environment_vars(cfg).items())


def render_unit(cfg: PodConfig) -> str:
    return _UNIT_TEMPLATE.format(
        kirocrew_bin=_kirocrew_bin(),
        unit_prefix=cfg.unit_prefix,
        environment=_environment_block(cfg),
    )


def unit_path(cfg: PodConfig) -> Path:
    """Where the template unit is installed for the current user."""
    return Path.home() / ".config" / "systemd" / "user" / f"{cfg.unit_prefix}@.service"


def install_unit(cfg: PodConfig) -> Path:
    """Write the template unit and return its path. Caller runs daemon-reload."""
    dst = unit_path(cfg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render_unit(cfg))
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
            exe = line[len("ExecStart="):].split()[0]
            return os.access(exe, os.X_OK) if os.path.isabs(exe) else True
    return False


# Directives an older installed unit may still carry that this build has removed.
# ``ExecStopPost`` ran teardown before systemd's final kill of the pod's cgroup,
# so it raced the pod's own subprocesses and also wiped the HOME on the stop half
# of a ``Restart=``; reclamation now belongs to the ``down`` path.
_REMOVED_DIRECTIVES = ("ExecStopPost=",)


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
    return not any(
        line.startswith(directive)
        for line in text.splitlines()
        for directive in _REMOVED_DIRECTIVES
    )

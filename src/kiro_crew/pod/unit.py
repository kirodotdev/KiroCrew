"""systemd ``--user`` template unit for pods — generated, not shipped.

The unit's ``ExecStart`` re-enters the installed ``kirocrew`` binary as
``kirocrew pod _run %i``, so the boot logic lives in Python (see
:func:`kiro_crew.pod.runtime.boot`) and nothing is shipped outside the package.
``ExecStopPost`` re-enters ``kirocrew pod _cleanup %i`` to delete the pod's
isolated HOME for zero-residue teardown. Teardown goes through Python (not a raw
``rm -rf`` on ``%i``) because a systemd instance name can be ``..`` even though it
cannot contain ``/``; :func:`kiro_crew.pod.runtime.cleanup_home` re-validates the
name and refuses anything that isn't a single safe segment under the pod root.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from kiro_crew.pod.config import (
    DEFAULT_BASE_PORT,
    DEFAULT_LIVE_PORT,
    DEFAULT_UNIT_PREFIX,
    PodConfig,
)

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

# Zero-residue teardown: delete this pod's isolated HOME on stop. Routed through
# Python (pod _cleanup) which re-validates %i and refuses '..'/absolute/empty —
# the teardown safety must NOT rely on systemd %i semantics (%i can be '..').
ExecStopPost={kirocrew_bin} pod _cleanup %i

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
    """``Environment=`` lines for every config value that differs from the built-in
    default, so the systemd-booted gateway resolves the same plane the installing
    CLI used. Returns "" when everything is at defaults.
    """
    home = Path.home()
    candidates = [
        ("KIROCREW_POD_ROOT", str(cfg.pod_root), str(home / ".kirocrew-pods")),
        ("KIROCREW_POD_ENV_DIR", str(cfg.pods_dir), str(home / ".kirocrew" / "pods")),
        (
            "KIROCREW_POD_ARTIFACTS_DIR",
            str(cfg.artifacts_dir),
            str(cfg.pod_root / ".e2e-artifacts"),
        ),
        ("KIROCREW_POD_BASE_PORT", str(cfg.base_port), str(DEFAULT_BASE_PORT)),
        ("KIROCREW_POD_LIVE_PORT", str(cfg.live_port), str(DEFAULT_LIVE_PORT)),
        ("KIROCREW_POD_UNIT_PREFIX", cfg.unit_prefix, DEFAULT_UNIT_PREFIX),
        ("KIROCREW_POD_PATH", cfg.gateway_path, None),  # always pin PATH
    ]
    lines = [
        f"Environment={key}={val}\n"
        for key, val, default in candidates
        if default is None or val != default
    ]
    # Optional resolvers — pinned only when set. boot() normally reads the pinned
    # CHECKOUT= from the per-pod env file directly, so these are belt-and-braces so
    # a systemd-booted unit can still resolve the same repo/root the CLI used.
    if cfg.repo_hint is not None:
        lines.append(f"Environment=KIROCREW_POD_REPO={cfg.repo_hint}\n")
    if cfg.worktrees_root is not None:
        lines.append(f"Environment=KIROCREW_POD_WORKTREES_ROOT={cfg.worktrees_root}\n")
    return "".join(lines)


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

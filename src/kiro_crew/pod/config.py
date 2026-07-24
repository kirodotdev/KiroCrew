"""Pod runtime configuration — the single source of truth for every path and
knob the pod runtime uses, all derived from ``$HOME`` and overridable by env.

Every value is ``KIROCREW_POD_*``-overridable so a test harness can stand up a
fully hermetic pod plane (its own unit prefix, base port, and roots) that cannot
collide with a developer's live pods.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config.paths import _default_home

# The canonical live gateway port a pod must never bind. Overridable for hosts
# that run their live plane elsewhere.
DEFAULT_LIVE_PORT = 5476

# Pod ports are derived as BASE + (cksum(name) % 199) + 1, i.e. BASE+1 .. BASE+199.
DEFAULT_BASE_PORT = 7810

# systemd --user template unit. ``<prefix>@<wt>.service`` is one pod.
DEFAULT_UNIT_PREFIX = "kirocrew-pod"


def _env_path(key: str, default: Path) -> Path:
    val = os.environ.get(key)
    return Path(val).expanduser() if val else default


def _env_path_opt(key: str) -> Path | None:
    val = os.environ.get(key)
    return Path(val).expanduser() if val else None


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return default


@dataclass(frozen=True)
class PodConfig:
    """Resolved, immutable view of where pods live and how they are named.

    Build with :meth:`load` (reads the environment). All fields are plain paths /
    ints / strings so the rest of the runtime never re-reads ``os.environ``.
    Worktree *paths* are NOT stored here — a friendly name is resolved to a
    checkout by :func:`kiro_crew.pod.runtime.resolve_checkout` (git-native).
    """

    # Isolated pod HOMEs (one dir per running pod; nuked on stop).
    pod_root: Path
    # Per-pod env files (pinned CHECKOUT= / PORT= / SEED= for a named pod).
    pods_dir: Path
    # Artifacts (logs / screenshots) from pod runs.
    artifacts_dir: Path
    # Deterministic port base and the live port to refuse.
    base_port: int
    live_port: int
    # systemd unit naming.
    unit_prefix: str
    # PATH handed to a booted pod gateway.
    gateway_path: str
    # Optional: the repo git is queried from to resolve worktree names. When
    # unset, resolution falls back to the invoking working directory.
    repo_hint: Path | None
    # Optional: last-resort name->path root used only when git resolution can't
    # be used (hermetic test/CI planes). Unset by default — git is primary.
    worktrees_root: Path | None

    @classmethod
    def load(cls) -> "PodConfig":
        home = Path.home()
        pod_root = _env_path("KIROCREW_POD_ROOT", home / ".kirocrew-pods")
        # Pod env files are HOST-side state, so they follow the data-home move to
        # ~/.kiro/crew. Use the DEFAULT home (not config_dir()) so a pod process
        # that has its own isolated KIROCREW_HOME set can't redirect the host's
        # pod registry into the pod's throwaway home.
        pods_dir = _env_path("KIROCREW_POD_ENV_DIR", _default_home() / "pods")
        artifacts_dir = _env_path("KIROCREW_POD_ARTIFACTS_DIR", pod_root / ".e2e-artifacts")
        default_path = os.pathsep.join(
            [
                str(home / ".local" / "bin"),
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
            ]
        )
        return cls(
            pod_root=pod_root,
            pods_dir=pods_dir,
            artifacts_dir=artifacts_dir,
            base_port=_env_int("KIROCREW_POD_BASE_PORT", DEFAULT_BASE_PORT),
            live_port=_env_int("KIROCREW_POD_LIVE_PORT", DEFAULT_LIVE_PORT),
            unit_prefix=os.environ.get("KIROCREW_POD_UNIT_PREFIX", DEFAULT_UNIT_PREFIX),
            gateway_path=os.environ.get("KIROCREW_POD_PATH", default_path),
            repo_hint=_env_path_opt("KIROCREW_POD_REPO"),
            worktrees_root=_env_path_opt("KIROCREW_POD_WORKTREES_ROOT"),
        )

    # ---- derived locations -------------------------------------------------
    def home_dir(self, name: str) -> Path:
        """Isolated ``KIROCREW_HOME`` for pod *name* (nuked on stop)."""
        return self.pod_root / name

    def env_file(self, name: str) -> Path:
        """Per-pod env file holding pinned ``CHECKOUT=`` / ``PORT=`` / ``SEED=``."""
        return self.pods_dir / f"{name}.env"

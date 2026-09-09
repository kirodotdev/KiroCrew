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

#: Boot exit codes a RESTART CANNOT HEAL, so the unit must not retry them.
#:
#: The pod unit is ``Restart=on-failure`` + ``RestartSec=5``. Every refusal in
#: :func:`kiro_crew.pod.runtime.boot` is a standing condition -- no pinned
#: checkout, no venv, no built dist, the derived port IS the live plane, or a pod
#: OS-home component that could not be verified -- so retrying re-runs the same
#: refusal every 5 seconds, forever, and buries the FATAL line under repeats.
#: ``pod/unit.py`` feeds these into ``RestartPreventExitStatus`` so the unit goes
#: ``failed`` and STAYS there with the reason visible.
#:
#: Lives here rather than in ``runtime`` because ``runtime`` imports ``unit`` (so
#: ``unit`` cannot import ``runtime`` back) while both already import this module
#: -- one definition, no drift between the code that returns a value and the unit
#: that exempts it.
EXIT_PROVISIONING = 3
EXIT_LIVE_PORT_COLLISION = 70
#: A security invariant the pod could not establish (currently: the OS home whose
#: components must all be link-checked before it becomes the child's ``HOME``).
EXIT_REFUSED_UNRECOVERABLE = 78
TERMINAL_BOOT_EXIT_CODES: tuple[int, ...] = (
    EXIT_PROVISIONING,
    EXIT_LIVE_PORT_COLLISION,
    EXIT_REFUSED_UNRECOVERABLE,
)


def _canonical_override(raw: str) -> Path:
    """An operator-supplied path override, anchored so every reader agrees.

    ``expanduser`` then ``abspath``. The second half is the load-bearing one: a
    RELATIVE override resolves against the reading process's working directory,
    and the two processes that must agree on a pod plane do not share one. The CLI
    runs from wherever the operator invoked it; the service-manager-booted gateway
    runs from whatever the unit specifies, and the pod unit deliberately sets no
    ``WorkingDirectory`` (see ``pod/unit.py``), so systemd hands it ``/``. With the
    value carried through verbatim, ``KIROCREW_POD_ROOT=tmp/pods`` therefore named
    ``$PWD/tmp/pods`` to the CLI and ``/tmp/pods`` to the gateway -- two different
    directories under one name.

    That split is a CREDENTIAL leak, not merely a confusing path. ``pod down``
    reclaims the tree the CLI resolves, while the pod's kiro-cli child mints its
    MCP OAuth grants under the tree the GATEWAY resolved (``KIROCREW_OS_HOME``,
    derived from ``pod_root``), so teardown swept a directory the grants were never
    in and they outlived the pod on the real machine -- the exact durable
    machine-level grant writer the pod-scoping work exists to prevent.

    Anchored HERE, at the producer, because this is the one place both sides come
    from: :meth:`PodConfig.load` reads it and :func:`environment_vars` serialises
    what ``load`` resolved, so pinning it once makes the unit carry an absolute
    path and every consumer inherit the agreement. Fixing it in a consumer instead
    would leave each new reader to re-derive the rule.

    ``abspath`` rather than ``Path.resolve()``, deliberately. Both would close the
    relative case; ``resolve()`` additionally rewrites the operator's symlink
    spelling, which is not the defect (two spellings of one directory still name
    one inode, so teardown scope is unaffected) and which would silently change
    the value pinned into a unit -- on macOS every ``/tmp/...`` plane becomes
    ``/private/tmp/...``. ``abspath`` normalises ``.``/``..`` and anchors to the
    CWD without touching links, and is the same primitive
    ``security._resolved_env_root`` falls back to.

    Only an OVERRIDE is anchored; a built-in default is already ``$HOME``-rooted
    and is left exactly as it was, which is what keeps :func:`environment_vars`
    emitting ``{}`` for an all-defaults plane on a host whose ``$HOME`` is a link.
    """
    return Path(os.path.abspath(os.path.expanduser(raw)))


def _env_path(key: str, default: Path) -> Path:
    val = os.environ.get(key)
    return _canonical_override(val) if val else default


def _env_path_opt(key: str) -> Path | None:
    val = os.environ.get(key)
    return _canonical_override(val) if val else None


def _env_int(key: str, default: int) -> int:
    val = os.environ.get(key)
    if val and val.strip().lstrip("-").isdigit():
        return int(val.strip())
    return default


def environment_vars(cfg: PodConfig) -> dict[str, str]:
    """The pod-plane env a service-manager-booted gateway must be given.

    A booted pod starts with a clean environment, so the plane the CLI resolved
    has to be pinned into the service definition or the gateway would resolve a
    DIFFERENT ``PodConfig`` than the CLI that started it. Only values that differ
    from the built-in default are emitted (plus ``KIROCREW_POD_PATH``, always
    pinned), so an all-defaults plane yields ``{}``.

    This is the *selection*, deliberately separated from the *serialisation*: the
    systemd backend renders these as ``Environment=K=V`` lines, the launchd
    backend hands the same dict to the plist's ``EnvironmentVariables`` key.
    Keeping one source of truth is what stops the two backends drifting into
    booting pods with different planes.
    """
    home = Path.home()
    candidates: list[tuple[str, str, str | None]] = [
        ("KIROCREW_POD_ROOT", str(cfg.pod_root), str(home / ".kirocrew-pods")),
        ("KIROCREW_POD_ENV_DIR", str(cfg.pods_dir), str(_default_home() / "pods")),
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
    out = {key: val for key, val, default in candidates if default is None or val != default}
    # Optional resolvers — pinned only when set. boot() normally reads the pinned
    # CHECKOUT= from the per-pod env file directly, so these are belt-and-braces
    # so a booted service can still resolve the same repo/root the CLI used.
    if cfg.repo_hint is not None:
        out["KIROCREW_POD_REPO"] = str(cfg.repo_hint)
    if cfg.worktrees_root is not None:
        out["KIROCREW_POD_WORKTREES_ROOT"] = str(cfg.worktrees_root)
    # Named for what the GATEWAY reads, not for the knob that set it: the pod's
    # gateway resolves its agent backend from KIROCREW_KIRO_BIN, so pinning any
    # other key would land in the service definition and change nothing. Emitted
    # only when the plane asked for it, so a default plane is byte-identical.
    if cfg.kiro_bin is not None:
        out["KIROCREW_KIRO_BIN"] = str(cfg.kiro_bin)
    return out


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
    # Optional: the agent backend a booted pod's gateway spawns, pinned into the
    # service definition as KIROCREW_KIRO_BIN. Unset by default, so an ordinary
    # pod resolves the host's real kiro-cli exactly as before.
    #
    # Exists because a booted pod starts from the service manager's clean
    # environment, so nothing a caller exports reaches it: `pod up` could not
    # hand a pod the packaged fake ACP backend, and an offline CI runner had no
    # way to drive a real agent turn inside a pod at all. Pinned through
    # `environment_vars` rather than either backend directly, so the systemd unit
    # and the launchd plist cannot drift on it.
    #
    # Defaulted, unlike its optional siblings above, so an existing
    # ``PodConfig(...)`` call site keeps compiling: this knob adds a capability
    # and every plane that does not ask for it must stay byte-identical.
    kiro_bin: Path | None = None

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
            kiro_bin=_env_path_opt("KIROCREW_POD_KIRO_BIN"),
        )

    # ---- derived locations -------------------------------------------------
    def home_dir(self, name: str) -> Path:
        """Isolated ``KIROCREW_HOME`` for pod *name* (nuked on stop)."""
        return self.pod_root / name

    def env_file(self, name: str) -> Path:
        """Per-pod env file holding pinned ``CHECKOUT=`` / ``PORT=`` / ``SEED=``."""
        return self.pods_dir / f"{name}.env"

    def refusal_file(self, name: str) -> Path:
        """Where a TERMINAL boot refusal is recorded for pod *name*.

        Host-side (beside the env file), deliberately NOT under
        :meth:`home_dir`: the refusal this records is precisely "a component of
        that tree could not be verified", so the tree is the wrong place to keep
        the evidence -- and ``down`` nukes the home, which would erase it.

        Exists because the two service managers make a refusal visible in
        different amounts. systemd leaves the unit ``failed`` with the ``FATAL``
        line in ``journalctl``. launchd has no journal and, under the exit-0
        mechanism that stops its restart loop (see
        :func:`kiro_crew.pod.launchd.launchd_exit_code`), a terminal refusal
        looks like an ORDINARY CLEAN EXIT to ``launchctl``. This file is what
        keeps it legible on both: ``kirocrew pod ls`` reads it through
        :func:`kiro_crew.pod.cli._print_refusals` and reports the refusal as its
        own section, so a refused pod does not silently drop out of the listing
        for merely not running. The ``--json`` form of ``ls`` reports live pods
        only and does not carry these records.
        """
        return self.pods_dir / f"{name}.refused"

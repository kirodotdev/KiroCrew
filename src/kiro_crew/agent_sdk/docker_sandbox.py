"""Docker-container confinement for the OpenCode ACP harness.

Windows has no Crew OS sandbox backend (no user namespaces, no ``sandbox-exec``),
so an enforced adapter cannot receive the OS credential mask ``wrap_argv`` would
otherwise apply -- and ``credential_mask_applies`` deliberately fails closed
there rather than tracking a mutable opt-in across the preflight/spawn window.
This module is the THIRD answer beside "refuse" and "run unfenced": run the
adapter inside a Docker container whose host mounts are exactly the session
workspace (read-write, so the agent can edit files), the sandbox state dir
(read-write, under the data home, so sessions persist across ``--rm`` runs),
the operator's OpenCode config dir (read-only, when one exists, so the session
resolves the same settings the host would) and the operator's OpenCode auth
file (read-only, when one exists, so the adapter can sign in -- absent for a
locally served model, which needs no sign-in). No credential home is visible
inside, which IS the mask, enforced by the container boundary instead of by a
Crew wrapper.

Scope is deliberately narrow (v1): the OpenCode harness only, enabled only by
the explicit ``agent.sandbox_docker`` opt-in, default false on every platform.
Nothing here changes any other harness's spawn, and a host without Docker (or
without the image) is refused with a remedy, never started unfenced: both the
preflight gate (``agent_sdk/tool_gate.py``) and the spawn site
(``acp/client.py``) resolve this independently, and the spawn re-probes fresh
(see ``use_cache``) so a daemon or image that disappears between the two fails
the session instead of downgrading it.

Deliberate non-goals, stated so a reader does not file them twice: the
container runs as root with dropped capabilities (file ownership on
Docker Desktop mounts stays writable that way); SSH keys and agent sockets are
NEVER mounted, so git-over-ssh to a private remote fails inside by design and
https remotes are the supported shape; ACP stdio intentionally has no TTY
(``-i`` without ``-t``); CPU is unbounded (host core counts vary too widely to
pick a safe cap -- memory and pids bound the dangerous shapes) and disk has no
per-container quota (Docker cannot express one portably -- ``/tmp`` is a tmpfs
so temp counts against the memory ceiling instead of the overlay).

A LEAF module, deliberately: it imports the harness vocabulary from
:mod:`kiro_crew.agent_sdk.backends` and lazily from ``config.loader`` (flag)
and ``config.paths`` (state dir, which honours ``KIROCREW_HOME``), and nothing
from ``kiro_crew.acp``, so the SDK boundary gate keeps holding and the
preflight gate can reach the predicate without buying a forbidden edge.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Mapping

from kiro_crew.agent_sdk.backends import ACP_BACKEND_OPENCODE, launch_for

logger = logging.getLogger(__name__)

#: The container image that carries the adapter. Built once by the operator
#: from the shipped Dockerfile (see :func:`build_command`); never pulled, so a
#: missing image fails fast with a build remedy instead of reaching for a
#: registry at session-start time.
IMAGE_REF = "kirocrew-opencode:latest"

#: Where the session workspace lands inside the container. A fixed path, not a
#: translation of the host path: a Windows ``C:\\...`` work dir has no Linux
#: spelling, so tool output names this path. Both the config read-back and the
#: session spawn use the same mapping, so the read-back vouches for the exact
#: tree the session sees.
CONTAINER_WORKDIR = "/workspace"

#: HOME inside the container, and where the auth file lands under it. The auth
#: file is the ONE credential the adapter may read -- the mirror of
#: ``adapter_expose_files`` -- mounted read-only; every other credential home
#: is absent because it is never mounted.
CONTAINER_HOME = "/root"
CONTAINER_AUTH_PATH = "/root/.local/share/opencode/auth.json"
CONTAINER_STATE_PATH = "/root/.local/share/opencode"
CONTAINER_CONFIG_PATH = "/root/.config/opencode"
CONTAINER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

#: Container temp and scratch. The session env's host temp pointers are
#: meaningless inside (a Windows ``C:\\...`` temp has no Linux spelling), so
#: they are re-pinned here rather than dropped: ``tempfile``/``mktemp`` land on
#: the tmpfs ``/tmp`` (see :func:`docker_argv`), and the prompt-visible
#: ``KIROCREW_SCRATCH`` work area lands beside it. Both die with the container,
#: which matches the session lifetime.
CONTAINER_TMP = "/tmp"
CONTAINER_SCRATCH_PATH = "/tmp/kirocrew-scratch"

#: Top-level dir under the data home holding this sandbox's persistent state.
#: The container's opencode data dir (sessions, tool output) mounts from
#: ``<data home>/opencode-sandbox/state``, so a ``session/load`` in a fresh
#: ``--rm`` container finds the sessions an earlier one wrote. Operator data,
#: not credentials: the auth file overlays it read-only at
#: :data:`CONTAINER_AUTH_PATH`.
SANDBOX_STATE_DIR_NAME = "opencode-sandbox"

#: Host-path pointers that must never cross into the container environment.
#: The caller already scrubs credential VALUES (``scrub_agent_subprocess_env``);
#: these are the POINTERS -- a host HOME/TEMP/PATH is meaningless to a Linux
#: container at best, and at worst redirects config resolution (``XDG_*``,
#: ``OPENCODE_CONFIG``) or the interpreter lookup (``KIROCREW_RUNTIME_PYTHON``)
#: to paths that do not exist inside. ``KIRO_CHAT_LOG_FILE`` names a host
#: kiro-cli log path and no kiro-cli runs in this image, so it is dropped
#: rather than translated. Compared case-insensitively: Windows environment
#: keys arrive in whatever case the operator's shell used.
#:
#: ``TMP``/``TEMP``/``TMPDIR`` are dropped here AND re-pinned to
#: :data:`CONTAINER_TMP` by :func:`container_env_from` (a host temp has no
#: container spelling); ``KIROCREW_SCRATCH`` is prefix-dropped in the same pass
#: and likewise re-pinned to :data:`CONTAINER_SCRATCH_PATH`.
_DROP_EXACT = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SSH_AUTH_SOCK",
        "KRB5CCNAME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "OPENCODE_CONFIG",
        "KIROCREW_RUNTIME_PYTHON",
        "KIRO_CHAT_LOG_FILE",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
    }
)

#: Host bookkeeping prefixes with no meaning inside the container.
_DROP_PREFIXES = ("KIROCREW_", "DOCKER_")

#: Resource ceilings that replace the cgroup scope on hosts where it cannot run
#: (Windows has no cgroup delegation): a fork bomb and an RSS balloon are
#: container problems now, bounded here rather than by the gateway's scope.
#: ``/tmp`` is a tmpfs so temp counts against the memory ceiling instead of
#: growing the overlay unbounded. CPU is deliberately unbounded (see module
#: docstring) and disk has no portable per-container quota.
CONTAINER_PIDS_LIMIT = "512"
CONTAINER_MEMORY = "4g"

#: Hardening flags that need no capability the image uses: the adapter serves
#: ACP over stdio and needs no setuid path and no low-port bind, so new
#: privileges are denied and every capability is dropped. The network stays
#: open -- the agent needs model TLS -- so isolation remains mount-absence,
#: and these flags only shrink what a compromised in-container process can do
#: with it.
CONTAINER_CAP_DROP = "ALL"

#: Probe budget. The daemon/image checks shell out to the ``docker`` CLI, so
#: they run off the event loop at every call site and cache briefly: a cold
#: Docker Desktop can take seconds to answer, and the preflight plus the spawn
#: plus the read-back would otherwise pay that three times per session. The
#: spawn site bypasses the cache (``use_cache=False``): the preflight verdict
#: is for UX speed, but the spawn verdict is the security one, and a cached
#: "ready" must never start a container whose daemon died since the gate ran.
#: A stale verdict there still fails closed -- ``docker run`` exits non-zero
#: and no host spawn follows -- but the fresh probe names the cause instead.
_PROBE_TTL_S = 60.0
_PROBE_TIMEOUT_S = 15

_daemon_cache: tuple[float, bool] | None = None
_image_cache: tuple[float, bool] | None = None
_ostype_cache: tuple[float, str | None] | None = None


def dockerfile_path() -> Path:
    """The shipped Dockerfile the operator builds :data:`IMAGE_REF` from.

    Resolved beside this module, so it holds both from a source checkout and
    from an installed wheel (shipped as package data, not a repo-relative
    path that would silently vanish outside a checkout).
    """
    return Path(__file__).with_name("docker") / "Dockerfile.opencode"


def build_command() -> list[str]:
    """The exact ``docker build`` that produces :data:`IMAGE_REF`.

    Surfaced in refusals so a missing image names its own remedy; kept here
    rather than in prose so the command and the Dockerfile path cannot drift.
    """
    dockerfile = dockerfile_path()
    return [
        "docker",
        "build",
        "-t",
        IMAGE_REF,
        "-f",
        os.fspath(dockerfile),
        os.fspath(dockerfile.parent),
    ]


def _run_docker(args: list[str], timeout: int | float) -> subprocess.CompletedProcess[str] | None:
    """Run the ``docker`` CLI, returning ``None`` instead of raising, ever.

    A missing binary, a stopped daemon and a timeout all read as "no", which
    is the fail-closed direction: every caller treats ``None`` as unavailable.
    """
    if shutil.which("docker") is None:
        return None
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def daemon_reachable(use_cache: bool = True) -> bool:
    """Whether a Docker daemon answers, cached briefly (see ceilings above).

    The spawn site passes ``use_cache=False``: its verdict is the security
    one, so it pays the probe rather than trusting a gate-time answer.
    """
    global _daemon_cache
    now = time.monotonic()
    if use_cache and _daemon_cache is not None and now - _daemon_cache[0] < _PROBE_TTL_S:
        return _daemon_cache[1]
    completed = _run_docker(["info", "--format", "{{.ServerVersion}}"], _PROBE_TIMEOUT_S)
    reachable = completed is not None and completed.returncode == 0
    _daemon_cache = (now, reachable)
    return reachable


def daemon_ostype(use_cache: bool = True) -> str | None:
    """The daemon's ``OSType`` (``"linux"``/``"windows"``), or ``None``.

    ``None`` covers every unknown: no binary, no daemon, a timeout, and an
    unparseable answer. Callers treat a KNOWN non-Linux type as a refusal with
    a remedy (switch Docker Desktop to Linux containers); an unknown type
    proceeds, with the ``docker run`` of a Linux image as the fail-closed
    backstop rather than a refusal on inconclusive evidence.
    """
    global _ostype_cache
    now = time.monotonic()
    if use_cache and _ostype_cache is not None and now - _ostype_cache[0] < _PROBE_TTL_S:
        return _ostype_cache[1]
    completed = _run_docker(["info", "--format", "{{.OSType}}"], _PROBE_TIMEOUT_S)
    ostype: str | None = None
    if completed is not None and completed.returncode == 0:
        ostype = completed.stdout.strip().lower() or None
    _ostype_cache = (now, ostype)
    return ostype


def image_available(use_cache: bool = True) -> bool:
    """Whether :data:`IMAGE_REF` exists locally. Never pulls (see module doc).

    The spawn site passes ``use_cache=False``, for the reason on
    :func:`daemon_reachable`.
    """
    global _image_cache
    now = time.monotonic()
    if use_cache and _image_cache is not None and now - _image_cache[0] < _PROBE_TTL_S:
        return _image_cache[1]
    completed = _run_docker(["image", "inspect", IMAGE_REF], _PROBE_TIMEOUT_S)
    available = completed is not None and completed.returncode == 0
    _image_cache = (now, available)
    return available


def sandbox_docker_enabled() -> bool:
    """Whether the operator explicitly opted into Docker confinement.

    A plain read of ``agent.sandbox_docker`` (default false everywhere): unlike
    the unsandboxed-exec flag this key has NO platform default to preserve, so
    presence carries nothing and the value is the whole policy. Never raises:
    an unreadable config reads as disabled.
    """
    try:
        from kiro_crew.config.loader import (  # circular import: leaf of config consumers
            KiroCrewConfig,
        )

        return bool(getattr(KiroCrewConfig.load().agent, "sandbox_docker", False))
    except Exception:
        return False


def check_docker_sandbox(use_cache: bool = True) -> str | None:
    """The Docker-confinement verdict: ``None`` when ready, else the reason.

    One ordered check so the preflight gate and the spawn site cannot disagree
    about WHY a session was refused: the flag first (a config answer), then the
    daemon (a host answer), then its container OS (a host answer with a switch
    remedy), then the image (a setup answer, naming its remedy). The config
    path is deliberately unnamed: it is platform-dependent (honours
    ``KIROCREW_HOME``), and the key name is the actionable part.
    """
    if not sandbox_docker_enabled():
        return "agent.sandbox_docker is not enabled in config.json"
    if not daemon_reachable(use_cache=use_cache):
        return "no reachable Docker daemon (is Docker Desktop running?)"
    ostype = daemon_ostype(use_cache=use_cache)
    if ostype is not None and ostype != "linux":
        return (
            "Docker daemon is in {}-container mode (OSType={!r}); switch Docker "
            "Desktop to Linux containers for the sandbox image".format(ostype, ostype)
        )
    if not image_available(use_cache=use_cache):
        return "Docker image '{}' is not built locally; build it with: {}".format(
            IMAGE_REF, " ".join(build_command())
        )
    return None


def docker_sandbox_applies(backend: str, use_cache: bool = True) -> bool:
    """Whether *backend* spawns Docker-confined right now. Never raises.

    Positive harness identity (``== ACP_BACKEND_OPENCODE``, H5): v1 confines
    one harness, and a new harness is added by naming it, never by failing to
    exclude it. The spawn site passes ``use_cache=False``.
    """
    if backend == ACP_BACKEND_OPENCODE:
        try:
            return check_docker_sandbox(use_cache=use_cache) is None
        except Exception:
            return False
    return False


def resolve_adapter_confinement(backend: str, mode: str, use_cache: bool = True) -> str:
    """One confinement verdict for the gate and the spawn: native/docker/refused.

    ``"native"`` when the Crew OS mask will be applied (the established path,
    unchanged); ``"docker"`` when only container confinement holds; ``"refused"``
    otherwise. The gate and the spawn resolve this independently at their own
    moment, and the spawn passes ``use_cache=False`` so a flag/daemon/image
    that flaps between the two fails the session instead of downgrading it:
    the spawn only ever runs the path whose condition still holds, and raises
    on ``"refused"``. Never raises itself.
    """
    try:
        from kiro_crew.sandbox import (  # circular import: sandbox is low-level
            credential_mask_applies,
        )

        if bool(credential_mask_applies(mode)):
            return "native"
    except Exception:
        pass
    if docker_sandbox_applies(backend, use_cache=use_cache):
        return "docker"
    return "refused"


def host_auth_file() -> str | None:
    """The operator's OpenCode auth file on this host, if one exists.

    The single credential bind (read-only) the container receives -- the same
    leaf ``host_auth`` names for the host-side expose. Honours the
    ``XDG_DATA_HOME`` override the auth declaration records (it replaces the
    ``.local/share`` prefix, so the relocated file keeps ``opencode/auth.json``
    under it); otherwise the ``$HOME``-rooted default. Absent when the operator
    signs in another way (e.g. a locally served model named in the project's
    ``opencode.json``, which needs no sign-in), in which case the container
    gets no auth bind at all rather than an empty file.
    """
    candidates: list[Path] = []
    try:
        override = os.environ.get("XDG_DATA_HOME")
        if override:
            candidates.append(Path(override) / "opencode" / "auth.json")
        candidates.append(Path.home() / ".local" / "share" / "opencode" / "auth.json")
    except Exception:
        return None
    for candidate in candidates:
        try:
            if candidate.is_file():
                return os.fspath(candidate)
        except OSError:
            continue
    return None


def host_config_dir() -> str | None:
    """The operator's OpenCode config dir on this host, if one exists.

    Mounted read-only (see :func:`docker_argv`) so the confined session
    resolves the same user settings the host would -- a default model, MCP
    servers, permissions -- while the host files stay unwritable. Honours
    ``XDG_CONFIG_HOME`` the way the harness does, else ``~/.config/opencode``.
    Absent (rather than an empty dir) when the operator keeps no user config.
    """
    candidates: list[Path] = []
    try:
        override = os.environ.get("XDG_CONFIG_HOME")
        if override:
            candidates.append(Path(override) / "opencode")
        candidates.append(Path.home() / ".config" / "opencode")
    except Exception:
        return None
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return os.fspath(candidate)
        except OSError:
            continue
    return None


def sandbox_state_dir() -> str | None:
    """Where the container's persistent opencode data lives on this host.

    ``<data home>/opencode-sandbox/state`` (honours ``KIROCREW_HOME`` through
    :func:`config.paths.data_home`): the read-write mount behind
    :data:`CONTAINER_STATE_PATH`, so sessions survive ``--rm`` runs and a
    ``session/load`` finds what an earlier container wrote. Operator session
    data, not credentials -- the auth file overlays it read-only. Never
    raises: an unresolvable home reads as "no state mount" rather than as a
    failed session.
    """
    try:
        from kiro_crew.config.paths import (  # leaf import: stdlib-only module
            data_home,
        )

        return os.fspath(data_home() / SANDBOX_STATE_DIR_NAME / "state")
    except Exception:
        return None


def ensure_sandbox_state_dir() -> str | None:
    """Create :func:`sandbox_state_dir` and return it, or ``None``.

    Blocking (mkdir); callers run it off the event loop. ``None`` means the
    session proceeds WITHOUT the state mount -- resume fidelity is lost but
    the security boundary (workspace/auth mounts) is unchanged, so this
    degrades the feature, never the confinement.
    """
    state = sandbox_state_dir()
    if state is None:
        return None
    try:
        Path(state).mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return state


def container_env_from(scrubbed_env: Mapping[str, str]) -> dict[str, str]:
    """Translate a scrubbed host env into the container's ``-e`` set.

    The input is the session env AFTER ``scrub_agent_subprocess_env`` -- no
    credential values reach here -- and this drops the host-path pointers on
    top (see :data:`_DROP_EXACT`), then pins the container's own HOME/PATH
    plus its temp and scratch. A host ``C:\\...`` temp has no container
    spelling, so ``TMP``/``TEMP``/``TMPDIR`` are re-pinned to
    :data:`CONTAINER_TMP` (the tmpfs ``/tmp``) instead of arriving dangling;
    ``KIROCREW_SCRATCH`` is re-pinned to :data:`CONTAINER_SCRATCH_PATH` so the
    prompt-visible work area resolves inside. Pure (no I/O), so the read-back
    and the spawn translate identically.
    """
    translated: dict[str, str] = {}
    for key, value in scrubbed_env.items():
        upper = key.upper()
        if upper in _DROP_EXACT:
            continue
        if upper.startswith(_DROP_PREFIXES):
            continue
        translated[key] = value
    translated["HOME"] = CONTAINER_HOME
    translated["USER"] = "root"
    translated["PATH"] = CONTAINER_PATH
    translated["TMPDIR"] = CONTAINER_TMP
    translated["TMP"] = CONTAINER_TMP
    translated["TEMP"] = CONTAINER_TMP
    translated["KIROCREW_SCRATCH"] = CONTAINER_SCRATCH_PATH
    return translated


def docker_argv(
    *,
    adapter_args: tuple[str, ...],
    work_dir: str,
    container_env: Mapping[str, str],
    auth_file: str | None,
    state_dir: str | None = None,
    config_dir: str | None = None,
) -> list[str]:
    """Build the ``docker run`` argv that confines one adapter invocation.

    Pure: no daemon contact, so unit tests pin the whole command without Docker
    installed. The confinement claim rests on what is ABSENT -- no credential
    mounts, no host env pointers (see :func:`container_env_from`), ``--pull
    never`` so a missing image fails instead of fetching -- rather than on any
    flag a reviewer must trust. ``state_dir`` (read-write, the persistent
    opencode data dir) and ``config_dir`` (read-only, the operator's user
    settings) are operator data, not credentials; neither widens the mask.
    Mount order is load-bearing where they overlap: the state dir first, then
    the auth file OVER it, so the read-only credential bind wins over the
    read-write data underneath.
    """
    work_abs = os.path.abspath(work_dir)
    argv: list[str] = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--init",
        "--cap-drop",
        CONTAINER_CAP_DROP,
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp",
        "--label",
        "kirocrew.docker-sandbox=opencode",
        "-w",
        CONTAINER_WORKDIR,
        "-v",
        "{}:{}".format(work_abs, CONTAINER_WORKDIR),
    ]
    if state_dir:
        argv += ["-v", "{}:{}".format(os.path.abspath(state_dir), CONTAINER_STATE_PATH)]
    if config_dir:
        argv += ["-v", "{}:{}:ro".format(os.path.abspath(config_dir), CONTAINER_CONFIG_PATH)]
    if auth_file:
        argv += ["-v", "{}:{}:ro".format(auth_file, CONTAINER_AUTH_PATH)]
    for key in sorted(container_env):
        argv += ["-e", "{}={}".format(key, container_env[key])]
    argv += [
        "--pids-limit",
        CONTAINER_PIDS_LIMIT,
        "--memory",
        CONTAINER_MEMORY,
        "--memory-swap",
        CONTAINER_MEMORY,
        IMAGE_REF,
        *adapter_args,
    ]
    return argv


def session_argv(
    *,
    work_dir: str,
    scrubbed_env: Mapping[str, str],
    auth_file: str | None,
    state_dir: str | None = None,
    config_dir: str | None = None,
) -> list[str]:
    """The containerized ``opencode acp`` session argv for *work_dir*.

    The adapter tail comes from the harness's own ``ACP_BACKEND_LAUNCH`` row,
    not a second spelling of it: the binary the row names is what runs inside
    the image (on the container PATH), followed by the row's ACP args. The
    caller resolves ``state_dir`` (via :func:`ensure_sandbox_state_dir`,
    off-loop) and ``config_dir`` (via :func:`host_config_dir`) so this stays
    pure -- and passes the SAME pair to the read-back argv, so the read-back
    vouches for the exact confined process the session runs.
    """
    launch = launch_for(ACP_BACKEND_OPENCODE)
    return docker_argv(
        adapter_args=(launch.binary, *launch.acp_args),
        work_dir=work_dir,
        container_env=container_env_from(scrubbed_env),
        auth_file=auth_file,
        state_dir=state_dir,
        config_dir=config_dir,
    )

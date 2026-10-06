"""Pod runtime core, and the ``kiro_crew.pod.runtime`` compatibility facade.

The core is what every other pod runtime owner builds on, and what repository gates
pin to this file: pod names and errors, the per-pod env file, git worktree
resolution, a pod's identity paths, the ``systemd --user`` adapter with the platform
dispatch in front of launchd and Task Scheduler, the per-name and plane-wide
lifecycle locks, seed sanitization, the pod gateway's environment, and the
owner-only directory helper. Everything that talks to the host lives in the pod
runtime so :mod:`kiro_crew.pod.cli` stays a thin verb layer, and no state is held:
each function reads what it needs from a :class:`PodConfig`.

The other owners import this core. The core imports them in turn only at the end
of its own body, once every name of its own is bound:

* :mod:`kiro_crew.pod.runtime_ports` -- port derivation and allocation
* :mod:`kiro_crew.pod.runtime_attestation` -- who serves a pod's port
* :mod:`kiro_crew.pod.runtime_client` -- health, credential mint, authenticated API
* :mod:`kiro_crew.pod.runtime_home` -- seeding, the OS home, HOME reclamation
* :mod:`kiro_crew.pod.runtime_lifecycle` -- start, stop, backend install
* :mod:`kiro_crew.pod.runtime_boot` -- boot, ``pod exec``, terminal refusals

Every name listed in :data:`_EXPORTS_BY_OWNER` is also reachable here: a read of
``kiro_crew.pod.runtime.<name>`` resolves on the owner, and a write or delete -- a
test's monkeypatch -- is forwarded to it, so the pod CLI, Dev Fleet and the pod
suite keep reaching one namespace. A name an owner adds is reachable here only once
it is listed in that table. A module the pod runtime imports (``time``,
``launchd``, ``pinned_fs``, ...) is one shared object, so patch its attributes
(``rt.time.sleep``). Rebinding the module name is refused, because each importing
module holds its own binding of it. See the facade at the end of this module.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.dashboard.urls import dashboard_socket_name
from kiro_crew.platform_compat import IS_LINUX, IS_MACOS, IS_WINDOWS, file_lock, open_lock_file
from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import windows as win_backend
from kiro_crew.pod.config import PodConfig
from kiro_crew.service.common import session_runtime_dir, systemctl_user_env
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.user_json import strip_utf8_bom

if TYPE_CHECKING:  # served by ``__getattr__`` at runtime; named here for mypy
    from kiro_crew.pod.runtime_attestation import (  # noqa: F401
        OWNER_FOREIGN,
        OWNER_POD,
        OWNER_UNPROVEN,
        attributed_descendants,
        find_port_listeners,
        listening_pid_tool_available,
        loopback_owner_pids,
        port_owner,
        process_start_time,
        run_marker,
    )
    from kiro_crew.pod.runtime_boot import (  # noqa: F401
        EXIT_PROVISIONING,
        EXIT_REFUSED_UNRECOVERABLE,
        TERMINAL_BOOT_EXIT_CODES,
        boot,
        exec_in_pod,
        pod_context,
        refusal_reason,
        require_pod_safe_verb,
        target_supports_flag,
        terminal_exit_code,
    )
    from kiro_crew.pod.runtime_client import (  # noqa: F401
        API_BODY_MAX_BYTES,
        API_METHODS,
        API_READ_METHODS,
        API_TIMEOUT_SECS,
        HEALTH_FOREIGN,
        api_path,
        get_peer_pid,
        health,
        http,
        loopback_urlopen,
        mint_token,
        pod_api,
        published_credential,
        unix_socket_urlopen,
    )
    from kiro_crew.pod.runtime_home import (  # noqa: F401
        SeedError,
        StoreMapping,
        atomic_write_at,
        cleanup_home,
        is_link_or_junction,
        is_scenario_ref,
        open_file_no_reparse,
        orphan_homes,
        pin_directory,
        resolve_seed_scenario,
        resolved_pod_home,
        seed_home_from_scenario,
        seed_mod,
        seeded_scenario_in_home,
        store_mappings,
        write_pod_config,
    )
    from kiro_crew.pod.runtime_lifecycle import (  # noqa: F401
        DRAIN_TIMEOUT_SECS,
        RECLAIMED_MARKER,
        cgroup_procs_file,
        drain_cgroup,
        halt_pod,
        install_backend,
        loaded_teardown_hook,
        start_pod,
        stop_pod,
        time,
        unit_mod,
    )
    from kiro_crew.pod.runtime_ports import (  # noqa: F401
        AUTO_PORT_KEY,
        allocate_port,
        derive_port,
        operator_pinned,
        socket,
    )

# Pod names become systemd instance names and path segments; keep them strict.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")


class PodError(RuntimeError):
    """A pod operation could not be completed (bad name, no worktree, mint failed…)."""


class PodBackendAbsent(PodError):
    """The pod service manager is provably not running on this host.

    Raised only when no user-bus address exists or a completed systemctl probe
    reports no user session. An executable, timeout, or other operational
    failure remains :class:`PodError`, so callers never infer that no live pod
    can exist from a probe they could not run.
    """


class PodOwnershipUnproven(PodError):
    """Ownership could not be PROVEN either way, so a credential was withheld.

    A distinct type because the two refusals want different handling. A
    :data:`OWNER_FOREIGN` verdict is positive knowledge that the port belongs to
    somebody else, and every caller should stop. "Could not prove it" is not that
    — the pod may well be serving — so a caller whose main job is something other
    than the credential (``pod up``, which has already booted the pod) can go on
    and report what it does know, while still never putting the secret on the
    wire. ``pod token`` has nothing else to do and still fails.
    """


def validate_name(name: str) -> str:
    if not name or not _NAME_RE.match(name):
        raise PodError(f"invalid pod name {name!r}")
    return name


# --------------------------------------------------------------------------- #
# Per-pod env file (pinned CHECKOUT= / PORT= / SEED= / APPROVAL= / CRONS= /
# EMBEDDINGS=). Values are single-quoted on write and unquoted on read; unknown
# keys are preserved on merge.
# --------------------------------------------------------------------------- #

# Approval modes a pod's gateway may boot with, mirroring the choices on
# ``kirocrew gateway --approval``. This tuple is the ENFORCEMENT point: the env
# file is hand-editable, so ``boot`` re-validates against it instead of trusting
# whatever ``pod up`` wrote. The top-level ``cli.py`` repeats the literal for its
# argparse ``choices`` because that parser deliberately imports no pod module at
# startup; argparse is the UX layer, this tuple is the invariant.
APPROVAL_MODES: tuple[str, ...] = ("reads", "yolo", "interactive")


# Truthy spellings accepted for the boolean ``CRONS=`` key. ``pod up --crons``
# writes ``"1"``; the others are accepted because the env file is hand-editable
# and these are the obvious alternatives. Anything else is treated as OFF, which
# is the pre-existing ``--no-crons`` behavior and the safer of the two.
CRONS_TRUE: frozenset[str] = frozenset({"1", "true", "yes", "on"})


# Falsy spellings accepted for the ``EMBEDDINGS=`` key. Note the inverted
# polarity against ``CRONS``: embeddings are ON for every pod that says nothing,
# so this key exists to express the OFF request and an unrecognised value leaves
# them ON -- the default, hence the safer answer, by the
# same reasoning ``CRONS`` uses for its own default. ``pod up --no-embeddings``
# writes ``"0"``; the rest are accepted because the file is hand-editable.
EMBEDDINGS_FALSE: frozenset[str] = frozenset({"0", "false", "no", "off"})


#: The env var that turns the embedding-model download off, restated here rather
#: than imported because this module runs on the gateway boot path and
#: :mod:`kiro_crew.embeddings` carries the vendored llama.cpp resolution with it --
#: the same boot-path reasoning :mod:`kiro_crew.pod.runtime_home` records for the
#: MCP OAuth grant names.
#: ``test_the_skip_download_env_name_matches_the_embedder`` pins it equal to the
#: embedder's own constant, so the two cannot drift.
SKIP_MODEL_DOWNLOAD_ENV = "KIROCREW_SKIP_MODEL_DOWNLOAD"


#: The embedder's two model-override variables, restated for the same reason
#: and pinned to its constants by
#: ``test_the_embed_model_override_env_names_match_the_embedder``.
#: :func:`build_pod_env` drops both alongside the skip switch: that switch gates
#: only the DOWNLOAD, and :func:`kiro_crew.embeddings.resolve_custom_model` reads
#: ``KIROCREW_EMBED_MODEL_PATH`` first -- so without this a pod booted without
#: embeddings loads the operator's custom GGUF and embeds anyway.
EMBED_MODEL_OVERRIDE_ENVS: tuple[str, ...] = (
    "KIROCREW_EMBED_MODEL_PATH",
    "KIROCREW_EMBED_MODEL_URL",
)


def embeddings_disabled(env_data: dict[str, str]) -> bool:
    """Does this pod's env file ask to boot WITHOUT the embedding model?

    Takes the already-parsed mapping rather than the pod name so the two
    application points -- the gateway's own boot and every ``pod exec``, which
    reaches the env through :func:`pod_context` -- share
    one parsing rule and cannot drift into disagreeing about the same file. Both
    already read that file for ``CHECKOUT``, so this adds no second read.
    """
    return env_data.get("EMBEDDINGS", "").strip().lower() in EMBEDDINGS_FALSE


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY='value'`` lines. Split out so a caller that must open the file
    itself -- see ``runtime_ports._read_peer_env``, which needs no-follow semantics -- can
    reuse this exact grammar instead of carrying a second copy that would drift."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, val = ln.split("=", 1)
        raw = val.strip()
        # Strip a single matched surrounding quote pair only (the form
        # write_env_file emits), so a value that legitimately contains a quote is
        # not mangled.
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        out[key.strip()] = raw
    return out


def read_env_file(cfg: PodConfig, name: str) -> dict[str, str]:
    """Parsed ``KEY='value'`` pairs for pod *name*, ``{}`` on any ``OSError``.

    Fail-OPEN by design, which bounds who may use it: a missing pod, an
    unreadable pods dir and a comments-only file all yield the same empty
    mapping, so a caller that must tell "absent" from "exists but cannot be
    positively read" MUST NOT read the pin through here -- see
    ``dev_fleet._read_pin_strict``, which propagates the failure instead.

    Takes the pod NAME, so the path read is one an operator named. A caller that
    instead reads whatever files happen to be in the pods directory is choosing its
    paths from directory contents rather than from an operator, which is a different
    trust posture -- ``runtime_ports._read_peer_env`` is that caller and does not
    come through here.
    """
    try:
        text = cfg.env_file(name).read_text()
    except OSError:
        return {}
    return _parse_env_text(text)


def write_env_file(cfg: PodConfig, name: str, updates: dict[str, str]) -> None:
    """Merge *updates* into the pod's env file, preserving existing keys.

    Values MUST be single-line: the ``KEY='value'`` format does not escape
    newlines, so a multi-line value would not round-trip. ``--seed`` is
    user-supplied, so reject a newline-bearing value loudly (fail-closed) rather
    than silently writing an un-parseable file.

    **Written atomically**, because readers are deliberately lock-free: ``boot``
    reads this file without taking the mutex (so ``pod up`` can hold it across
    the health wait without deadlocking against the process it waits for). An
    in-place truncating rewrite therefore has a window where a reader — a
    ``Restart=`` re-exec, say — sees a partial or empty file. A dropped
    ``APPROVAL`` is not a benign default: ``boot`` leaves ``approval_mode``
    unset, which falls through to ``cfg.agent.approval_mode`` and lands on
    auto-approve, the LEAST restrictive outcome. Temp-file + rename means an
    unlocked reader sees either the old file or the new one, never a torn one.

    The merge additionally re-acquires :func:`pod_name_mutex`, which every
    mutating pod path already holds at its call site. That is defense in depth
    for direct callers rather than a fix for a live race, and it mirrors what
    ``start_pod`` / ``stop_pod`` already do; reentrancy is what makes
    re-acquiring it inside an outer transaction safe.
    """
    for key, val in updates.items():
        if "\n" in val or "\r" in val:
            raise PodError(f"pod env value for {key!r} must be single-line")
    with pod_name_mutex(cfg, name):
        data = read_env_file(cfg, name)
        data.update(updates)
        for key, val in data.items():
            if "\n" in val or "\r" in val:
                raise PodError(f"pod env value for {key!r} must be single-line")
        cfg.pods_dir.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{k}='{v}'\n" for k, v in data.items())
        atomic_write(cfg.env_file(name), body, newline="")


def pin_checkout(cfg: PodConfig, name: str, checkout: Path) -> None:
    """Pin the resolved absolute checkout so the systemd-booted gateway (and any
    ``Restart=`` re-exec) resolves it without shelling git from a clean env."""
    write_env_file(cfg, name, {"CHECKOUT": str(checkout)})


# --------------------------------------------------------------------------- #
# Git-native worktree resolution. A friendly name maps to an absolute checkout
# via the pinned CHECKOUT=, else `git worktree list`, else an optional root.
# --------------------------------------------------------------------------- #
def _git_worktrees(ref: Path) -> dict[str, Path]:
    """Map ``{basename | branch | abspath -> checkout}`` for every linked worktree
    of the repo *ref* belongs to. Empty on any git error (not a repo / git absent).
    ``git worktree list`` from ANY linked worktree lists them all.
    """
    try:
        cp = subprocess.run(
            ["git", "-C", str(ref), "worktree", "list", "--porcelain"],
            capture_output=True,
            timeout=10,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if cp.returncode != 0:
        return {}
    out: dict[str, Path] = {}
    cur: Path | None = None
    for ln in cp.stdout.splitlines():
        if ln.startswith("worktree "):
            cur = Path(ln[len("worktree ") :].strip())
            out.setdefault(cur.name, cur)
            out.setdefault(str(cur), cur)
        elif ln.startswith("branch ") and cur is not None:
            br = ln[len("branch ") :].strip()
            if br.startswith("refs/heads/"):
                br = br[len("refs/heads/") :]
            out.setdefault(br, cur)
    return out


def resolve_checkout(
    cfg: PodConfig, name: str, *, cwd: Path | None = None, use_pin: bool = True
) -> Path:
    """Resolve a friendly worktree *name* to an absolute checkout path.

    Order: pinned ``CHECKOUT=`` (if the dir still exists) → ``git worktree list``
    (from ``KIROCREW_POD_REPO`` else *cwd*), matching a worktree's basename, then
    its branch (``name`` or ``feat/<name>``), then an exact path → optional
    ``KIROCREW_POD_WORKTREES_ROOT/name`` fallback → :class:`PodError`.
    """
    # 1. Pinned checkout (authoritative; the path boot() relies on).
    if use_pin:
        pinned = read_env_file(cfg, name).get("CHECKOUT")
        if pinned:
            p = Path(pinned).expanduser()
            if p.is_dir():
                return p

    # 2. Ask git. `ref` is the repo hint or the invoking working directory.
    ref = cfg.repo_hint or (cwd or Path.cwd())
    wts = _git_worktrees(ref)
    hit = wts.get(name) or wts.get(f"feat/{name}")
    if hit is not None:
        return hit

    # 3. Optional fixed-root fallback (hermetic test/CI planes; no git needed).
    if cfg.worktrees_root is not None:
        cand = cfg.worktrees_root / name
        if cand.is_dir():
            return cand

    raise PodError(
        f"no git worktree {name!r}. Create one for your branch:\n"
        f"  git worktree add ../{name} -b feat/{name} main\n"
        f"  (run `kirocrew pod up {name}` from inside a kirocrew checkout, "
        f"or set KIROCREW_POD_REPO to point at one)"
    )


def pod_unit(cfg: PodConfig, name: str) -> str:
    """systemd unit name for pod *name*."""
    return f"{cfg.unit_prefix}@{name}.service"


def pod_home(cfg: PodConfig, name: str) -> Path:
    return cfg.home_dir(name)


def pod_socket_path(cfg: PodConfig, name: str, port: int) -> Path:
    """Path of pod *name*'s private dashboard unix socket.

    The pod's gateway binds ``dashboard_socket_path(port)`` resolved against ITS
    ``KIROCREW_HOME``, which is :func:`pod_home` -- so the socket lands in the
    pod's isolated home, not in the host's data home. Calling
    ``dashboard_socket_path`` from here would resolve THIS process's home and
    name a socket the pod never binds, which is why only the file name comes from
    the shared definition and the directory comes from the pod.

    That directory is the security property: it is created owner-only
    (``make_owner_only_dir``) and the socket is ``chmod 0600``, so no other local
    user can answer here -- unlike the pod's TCP port, which any local user can
    bind once the pod releases it.
    """
    return pod_home(cfg, name) / dashboard_socket_name(port)


# --------------------------------------------------------------------------- #
# systemd --user helpers.
# --------------------------------------------------------------------------- #
# The runtime-dir and bus-pointer resolution is shared with the service module's
# user-scope verbs (`kiro_crew.service.common`): both spawn `systemctl --user`
# and both read a bus failure as a verdict about the host, so one resolver
# decides what environment such a spawn sees. Kept under the module-level names
# `pod/cli.py` and the tests reach for.
_session_runtime_dir = session_runtime_dir


def _address_socket_paths(address: str) -> list[str] | None:
    """Filesystem socket paths named by a D-Bus address, or ``None``.

    A D-Bus address is a semicolon-separated list of ``transport:key=value``
    entries whose values are percent-escaped. ``unix:path=`` is the only form
    that names something on the filesystem to check: ``unix:abstract=`` lives in
    the abstract namespace, and ``tcp:``/``unixexec:``/``autolaunch:`` are not
    filesystem objects at all. ``None`` means at least one entry cannot be
    checked, so the address as a whole carries no filesystem verdict.
    """
    entries = [entry for entry in address.split(";") if entry.strip()]
    if not entries:
        return None
    paths: list[str] = []
    for entry in entries:
        transport, _, arguments = entry.partition(":")
        if transport.strip() != "unix":
            return None
        path = None
        for pair in arguments.split(","):
            key, separator, value = pair.partition("=")
            if separator and key.strip() == "path":
                path = urllib.parse.unquote(value.strip())
                break
        if not path:
            return None
        paths.append(path)
    return paths


def session_bus_socket() -> str:
    """Path of the D-Bus socket that fronts this user's systemd instance.

    An explicitly-set ``DBUS_SESSION_BUS_ADDRESS`` that names a filesystem
    socket wins, so a refusal names the path it actually judged rather than a
    conventional one the caller never pointed at.
    """
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        paths = _address_socket_paths(address)
        if paths:
            return paths[0]
    return os.path.join(_session_runtime_dir(), "bus")


def has_session_bus() -> bool:
    """Whether a systemd user-bus address is available to probe.

    An explicitly-set ``DBUS_SESSION_BUS_ADDRESS`` is taken at face value because
    it may name a non-filesystem transport. Otherwise the conventional socket
    must exist. This is only the cheap availability hint; :func:`probe_user_bus`
    makes the authoritative connection attempt.

    Face value is deliberate and load-bearing beyond diagnosis: a stale explicit
    address must never be reported as a provably absent backend, because that is
    what authorizes destructive Dev Fleet worktree removal. Naming the stale case
    is :func:`user_bus_failure_message`'s job, and it keeps the operational
    classification untouched.
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    return os.path.exists(session_bus_socket())


USER_BUS_REACHABLE = "reachable"


USER_BUS_NO_SESSION = "no_session"


USER_BUS_SANDBOXED_AWAY = "sandboxed_away"


USER_BUS_ERROR = "error"


_USER_BUS_PROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class UserBusProbe:
    """One ``systemctl --user is-system-running`` reachability verdict."""

    status: str
    socket: str
    detail: str


# systemd's load path for SYSTEM units, highest precedence first. This is
# Table 1 ("Load path when running in system mode") of systemd.unit(5),
# transcribed in full and verified against the systemd 261 manual. `user@.service`
# is a system unit that PID 1 instantiates per uid, so the system table is the
# right one, not the user table.
#
# The list is deliberately the COMPLETE table rather than the directories that
# seem plausible for this unit. A short search path is a false "absent": the
# probe reports no per-user manager on a host that HAS one, and the caller then
# names a platform limit instead of telling the reader to start the manager.
# Transcribing the whole table makes completeness checkable against one document.
# `/lib/systemd/system` is not in Table 1; it is the pre-usr-merge location of
# `/usr/lib/systemd/system` and is kept for split-usr hosts.
#
# The order decides only WHICH path a message names, because presence is "any one
# of these exists". Naming the highest-precedence hit is the accurate choice.
#
# Known limitations, both from reading the load path rather than asking systemd
# for it. A MASKED template (an entry symlinked to `/dev/null`) reads as present,
# because `os.path.exists` follows the symlink and `/dev/null` exists; reporting
# that correctly needs real shadowing semantics, where a mask in a
# high-precedence directory suppresses a lower-precedence unit. And a host that
# sets `$SYSTEMD_UNIT_PATH` moves the load path out from under this list
# entirely. Both are deliberately out of scope: such a host was offered
# `enable-linger` before this probe existed, so its behaviour is unchanged rather
# than newly wrong. Asking `systemctl list-unit-files 'user@*.service'` at SYSTEM
# scope needs no user bus and would subsume this list, the env var and the mask
# case; that is the right shape and is tracked separately, not bolted onto a fix.
_SYSTEMD_SYSTEM_UNIT_DIRS = (
    "/etc/systemd/system.control",
    "/run/systemd/system.control",
    "/run/systemd/transient",
    "/run/systemd/generator.early",
    "/etc/systemd/system",
    "/etc/systemd/system.attached",
    "/run/systemd/system",
    "/run/systemd/system.attached",
    "/run/systemd/generator",
    "/usr/local/lib/systemd/system",
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    "/run/systemd/generator.late",
)

# Both shapes of per-user manager unit (the `user@.service` template and a
# hand-installed `user@<uid>.service`) are looked up through `_unit_paths` over
# the single `_SYSTEMD_SYSTEM_UNIT_DIRS` list above, never a second tuple, so the
# two searches cannot cover different directories.
_USER_MANAGER_TEMPLATE_NAME = "user@.service"


def _unit_paths(name: str) -> tuple[str, ...]:
    """Candidate paths for a unit ``name``, in systemd precedence order."""
    return tuple(f"{directory}/{name}" for directory in _SYSTEMD_SYSTEM_UNIT_DIRS)


def _user_manager_template() -> str | None:
    """Path of systemd's per-user manager template, or ``None`` when absent.

    Only the TEMPLATE. A host without it can still have a per-user manager, via a
    hand-installed ``user@<uid>.service``, so absence here is not the answer to
    "can this host run pods" -- :func:`user_manager_unit` is, and it checks both
    shapes. Enterprise Linux 7 derivatives (RHEL 7, CentOS 7, Amazon Linux 2) are
    the case that matters: they carry systemd 219 with the per-user manager left
    out, so neither shape is present and ``loginctl enable-linger`` has nothing to
    start.

    Probed by unit path rather than by asking ``systemctl``, because the question
    is whether the machinery EXISTS at all; asking the very tool that needs it
    returns the raw "Failed to connect to bus" that :func:`require_systemd` is
    there to translate. Deliberately NOT keyed on the bus socket either: a stray
    session ``dbus-daemon`` can create the socket on a host with no per-user
    manager, which makes the socket a false positive for "pods can run".
    """
    for path in _unit_paths(_USER_MANAGER_TEMPLATE_NAME):
        if os.path.exists(path):
            return path
    return None


def user_manager_unit(uid: int) -> str | None:
    """Path of a per-user manager unit this host could start, or ``None``.

    Wider than :func:`_user_manager_template` on purpose, and it is this — not the
    template alone — that answers "can this host have a ``systemd --user``
    instance". Two shapes count:

    * the template ``user@.service``, which logind instantiates per uid; and
    * a concrete ``user@<uid>.service`` installed directly.

    The second is not hypothetical: ``docs/guides/remote-and-mobile.md`` tells
    the reader to write ``/etc/systemd/system/user@$(id -u).service`` by hand and
    enable it, which is the documented way to get a per-user manager on a host
    whose distribution ships no template. systemd loads that concrete unit
    without ever consulting the template, so keying the platform gate on the
    template alone would refuse pods on a host that followed this repo's own
    guide and has a working instance.
    """
    template = _user_manager_template()
    if template is not None:
        return template
    # Callers resolve the uid, so a host without `os.getuid` (Windows) reaches
    # here with -1 rather than a uid this function invented.
    if uid < 0:
        return None
    for path in _unit_paths(f"user@{uid}.service"):
        if os.path.exists(path):
            return path
    return None


def _systemctl_env() -> dict[str, str]:
    """Environment for the pod runtime's ``systemctl --user`` spawns.

    The session-bus backfill is :func:`kiro_crew.service.common.systemctl_user_env`
    (why it exists and what it never overrides is documented there). Pods add one
    thing on top: Kiro Crew's CLI and error classifier use English diagnostics,
    so the service-manager tools are pinned to their stable C messages and a host
    locale cannot turn Permission denied or No medium found into the generic
    failure class.
    """
    env = systemctl_user_env()
    env["LC_ALL"] = "C"
    return env


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout,
        env=_systemctl_env(),
        **UTF8_TEXT,
    )


def probe_user_bus() -> UserBusProbe:
    """Classify whether this process can connect to the systemd user bus.

    A socket's existence proves only that a user manager created it. An outer
    sandbox can still deny ``connect(2)``, which is the failure this probe must
    keep distinct from a machine with no user manager at all. A nonzero
    ``is-system-running`` result with a state on stdout is reachable: systemd
    returns nonzero for valid states such as ``degraded``.

    A provably absent address needs no subprocess and is the only source of
    ``USER_BUS_NO_SESSION``. Once a probe is spawned, permission denial is the
    only separately classified failure; every other failure is operationally
    unknown, never proof that no backend exists.
    """
    sock = session_bus_socket()
    if not has_session_bus():
        return UserBusProbe(USER_BUS_NO_SESSION, sock, "")

    systemctl_bin = platform_compat.trusted_system_bin("systemctl")
    if systemctl_bin is None:
        return UserBusProbe(
            USER_BUS_ERROR,
            sock,
            "systemctl was not found in trusted system directories",
        )

    try:
        cp = _run(
            [systemctl_bin, "--user", "is-system-running"],
            timeout=_USER_BUS_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return UserBusProbe(
            USER_BUS_ERROR,
            sock,
            f"systemctl --user is-system-running timed out after {exc.timeout}s",
        )
    except OSError as exc:
        return UserBusProbe(USER_BUS_ERROR, sock, str(exc))

    stdout = (cp.stdout or "").strip()
    detail = (cp.stderr or cp.stdout or "").strip()
    lowered = detail.casefold()
    if "permission denied" in lowered or "eacces" in lowered:
        return UserBusProbe(USER_BUS_SANDBOXED_AWAY, sock, detail)
    if cp.returncode == 0 or stdout:
        return UserBusProbe(USER_BUS_REACHABLE, sock, detail)
    if not detail:
        detail = f"systemctl --user is-system-running exited {cp.returncode} without output"
    return UserBusProbe(USER_BUS_ERROR, sock, detail)


def _no_user_manager_remedy() -> str:
    """The remedy for a host with no per-user systemd instance running.

    ``loginctl`` talks to the SYSTEM bus, so it is not self-service on a host
    that cannot reach a bus at all, and ``sudo loginctl enable-linger <name>``
    still fails where root's name lookup does not resolve the account. Naming the
    privileged uid form and a preview path that needs no systemd leaves an actor
    who can act in every case.
    """
    uid = getattr(os, "getuid", lambda: -1)()
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid)
    return (
        f"Fix: loginctl enable-linger {user}\n"
        "If that command itself cannot connect to a bus, this shell cannot reach the "
        "system bus either, so it can never be the remedy from here: run it from a "
        "host shell, or have an administrator run "
        f"`sudo loginctl enable-linger {uid}` — the numeric uid resolves where a "
        "name lookup does not.\n"
        "To preview a worktree with no systemd at all, use `./dev-backend.sh`."
    )


def user_bus_failure_message(result: UserBusProbe) -> str:
    """Render one actionable pod error and retain any systemctl diagnostic."""
    raw = result.detail.strip()
    reason = raw.rsplit(":", 1)[-1].strip() if raw else "probe failed without a diagnostic"
    if result.status == USER_BUS_SANDBOXED_AWAY:
        message = (
            f"Cannot reach the systemd user bus at {result.socket} ({reason}). "
            "An outer layer, such as a container or launcher shim, blocks this "
            "process from reaching the user bus. Run pod commands from a host shell."
        )
    elif result.status == USER_BUS_NO_SESSION:
        message = (
            f"Cannot reach the systemd user bus at {result.socket} (no user session bus). "
            "Pods are systemd --user units, so one is required.\n" + _no_user_manager_remedy()
        )
    elif not os.path.exists(result.socket):
        # A probe ran and failed against a path that holds no socket. The class
        # stays operationally unknown — an explicit address is never proof that
        # no backend exists — but the remedy is the one a stopped per-user
        # manager needs, not an instruction to rerun the command that just
        # failed. A login session exports the address and a `Linger=no` manager
        # then stops at logout and deletes the socket, which is the usual state
        # on a Cloud Dev Desktop.
        message = (
            f"Cannot reach the systemd user bus at {result.socket} ({reason}). "
            "Nothing is listening at that path, so the address is stale: a per-user "
            "systemd instance is not running, and pods are systemd --user units.\n"
            + _no_user_manager_remedy()
        )
    else:
        message = (
            f"Cannot reach the systemd user bus at {result.socket} ({reason}). "
            "Run `systemctl --user is-system-running` from a host shell and fix that "
            "error before using pod commands."
        )
    if raw:
        return f"{message}\nRaw systemctl error: {raw}"
    return message


def require_systemd() -> None:
    """Raise :class:`PodError` unless cheap systemd prerequisites exist.

    Pods are Linux ``systemd --user`` only (see ``pod/README.md`` → Platform).
    This gate stays in-process because every systemd and journalctl helper calls
    it. The authoritative connection attempt belongs at :func:`require_backend`
    verb entry and in doctor, not before every unit query.

    A missing bus has two causes with different remedies. With a per-user
    manager unit installed, ``enable-linger`` starts the stopped instance.
    Without either a template or a concrete uid unit, there is no manager for
    linger to start: name the platform limit and point at ``dev-backend.sh``.
    """
    if not IS_LINUX:
        raise PodError(
            f"pods require Linux `systemctl --user`; this host is {sys.platform}. "
            "Use `./dev-backend.sh` to preview a worktree on this platform."
        )
    if shutil.which("systemctl") is None:
        raise PodError("pods require `systemctl --user`, but no `systemctl` was found on PATH.")
    if not has_session_bus():
        uid = getattr(os, "getuid", lambda: -1)()
        if user_manager_unit(uid) is None:
            raise PodBackendAbsent(
                "pods require `systemd --user`, which this host does not provide: no "
                "per-user manager unit is installed (looked for "
                f"{_USER_MANAGER_TEMPLATE_NAME} and user@{uid}.service under "
                f"{', '.join(_SYSTEMD_SYSTEM_UNIT_DIRS)}).\n"
                "Enterprise Linux 7 derivatives (RHEL 7, CentOS 7, Amazon Linux 2) ship "
                "systemd without it, so `loginctl enable-linger` cannot help: there is "
                "no unit for it to start.\n"
                "Use `./dev-backend.sh` to run a worktree gateway on this host."
            )
        raise PodBackendAbsent(
            user_bus_failure_message(UserBusProbe(USER_BUS_NO_SESSION, session_bus_socket(), ""))
        )


def require_backend() -> None:
    """Gate on whatever service manager THIS host uses for pods.

    Linux verb entries pay for one authoritative connection probe. Low-level
    systemctl helpers retain only :func:`require_systemd`'s cheap checks, so one
    verb cannot spawn a fresh five-second probe before every unit query.
    """
    if IS_MACOS:
        try:
            launchd.require_backend()
        except launchd.LaunchdError as exc:  # translate to the pod error type
            raise PodError(str(exc)) from exc
        return
    if IS_WINDOWS:
        try:
            win_backend.require_backend()
        except win_backend.WindowsTaskError as exc:  # translate to the pod error type
            raise PodError(str(exc)) from exc
        return

    require_systemd()
    result = probe_user_bus()
    if result.status == USER_BUS_REACHABLE:
        return
    error_type = PodBackendAbsent if result.status == USER_BUS_NO_SESSION else PodError
    raise error_type(user_bus_failure_message(result))


def systemctl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    require_systemd()
    systemctl_bin = platform_compat.trusted_system_bin("systemctl")
    if systemctl_bin is None:
        raise PodError(
            "pods require `systemctl --user`, but no systemctl executable was found "
            "in trusted system directories; refusing to resolve it from PATH."
        )
    return _run([systemctl_bin, "--user", *args], timeout=timeout)


def is_active(cfg: PodConfig, name: str) -> bool:
    if IS_MACOS:
        try:
            return launchd.is_active(cfg, name)
        except launchd.LaunchdError as exc:
            # Fail closed as the documented pod error, not a traceback: the
            # probe REFUSES to call a pod absent when launchctl cannot answer.
            raise PodError(str(exc)) from exc
    if IS_WINDOWS:
        return win_backend.is_active(cfg, name)
    cp = systemctl("is-active", "--quiet", pod_unit(cfg, name))
    return cp.returncode == 0


def main_pid(cfg: PodConfig, name: str) -> int | None:
    """PID of the pod's OWN gateway process, or ``None`` when it is not running.

    This is the pod's identity, and it is exact rather than approximate because
    of how a pod boots: the unit is ``Type=simple`` running ``kirocrew pod _run
    %i``, and :func:`_run` finishes with ``os.execve`` of the worktree's
    ``kirocrew gateway``. The gateway therefore REPLACES the unit's main process
    instead of being spawned beneath it, so ``MainPID`` names the very process
    that binds the pod's port — no descendant walk, no cgroup scan.

    Raises :class:`PodError` when the service manager could not be asked at all.
    That is deliberately a different answer from ``None``: "asked, and this pod
    has no process" is a fact a caller can act on (see :func:`port_owner`, where
    it is what lets a listener be attributed to somebody else), while "could not
    ask" must leave the question open. ``systemctl show`` prints ``MainPID=0``
    for a dead or unknown unit and still exits 0, so an output with no
    ``MainPID`` line at all is the honest signal that the query itself failed.

    Windows has no ``exec``, so the gateway there is the wrapper's CHILD rather
    than its replacement; :func:`kiro_crew.pod.windows.supervise_gateway` records
    a pid plus its creation identity that may be the launcher ancestor of the
    process binding the port. :func:`port_owner` accounts for that shape by
    proving ownership through attributed descendants.
    """
    if IS_MACOS:
        return launchd.main_pid(cfg, name)
    if IS_WINDOWS:
        return win_backend.main_pid(cfg, name)
    cp = systemctl("show", pod_unit(cfg, name), "-p", "MainPID")
    for ln in cp.stdout.splitlines():
        if ln.startswith("MainPID="):
            raw = ln.split("=", 1)[1].strip()
            pid = int(raw) if raw.isdigit() else 0
            return pid if pid > 0 else None
    raise PodError(
        f"could not read MainPID for pod {name!r} from systemctl "
        f"(rc={cp.returncode}): {(cp.stderr or cp.stdout or '').strip()}"
    )


def unit_state(cfg: PodConfig, name: str) -> tuple[str, int]:
    """(ActiveState, NRestarts) for the pod's unit — ("unknown", 0) on error.

    Lets the up-path tell a CRASHED/crash-looping worktree gateway (a broken
    build, import error, bad config) apart from one that is just slow to come up —
    so we fail fast with the gateway's own error instead of polling a dead unit
    for the full timeout.

    On macOS launchd exposes no restart counter; see
    :func:`kiro_crew.pod.launchd.unit_state` for how the crash signal is
    preserved without one. Windows Task Scheduler exposes neither a restart
    counter nor a restart policy, and its status output is localized; see
    :func:`kiro_crew.pod.windows.unit_state` for the two recorded facts the
    same signal is derived from there.
    """
    if IS_MACOS:
        return launchd.unit_state(cfg, name)
    if IS_WINDOWS:
        return win_backend.unit_state(cfg, name)
    cp = systemctl("show", pod_unit(cfg, name), "-p", "ActiveState", "-p", "NRestarts")
    state, restarts = "unknown", 0
    for ln in cp.stdout.splitlines():
        if ln.startswith("ActiveState="):
            state = ln.split("=", 1)[1].strip()
        elif ln.startswith("NRestarts="):
            val = ln.split("=", 1)[1].strip()
            if val.isdigit():
                restarts = int(val)
    return state, restarts


def recent_journal(cfg: PodConfig, name: str, lines: int = 30) -> str:
    """Tail the pod's log — surface a boot failure's real cause.

    launchd has no journal, so on macOS this tails the files the pod's plist
    routes stdout/stderr to. Task Scheduler has none either, so on Windows this
    tails the files the generated ``.cmd`` wrapper redirects into. Same
    contract, different mechanism.
    """
    if IS_MACOS:
        return launchd.recent_journal(cfg, name, lines=lines)
    if IS_WINDOWS:
        return win_backend.recent_journal(cfg, name, lines=lines)
    # journalctl is a sibling of systemctl, not routed through it — gate it too,
    # or this one call still raises a bare FileNotFoundError off-Linux.
    require_systemd()
    cp = subprocess.run(
        ["journalctl", "--user", "-u", pod_unit(cfg, name), "-n", str(lines), "--no-pager"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_systemctl_env(),
    )
    return cp.stdout


def active_names(cfg: PodConfig) -> set[str]:
    """Worktree names with an active pod unit (one cheap call)."""
    if IS_MACOS:
        try:
            return launchd.active_names(cfg)
        except launchd.LaunchdError as exc:
            raise PodError(str(exc)) from exc
    if IS_WINDOWS:
        return win_backend.active_names(cfg)
    pat = f"{cfg.unit_prefix}@*.service"
    cp = systemctl("list-units", pat, "--state=active", "--no-legend", "--plain", "--no-pager")
    rx = re.compile(rf"{re.escape(cfg.unit_prefix)}@(.+)\.service")
    names: set[str] = set()
    for ln in cp.stdout.splitlines():
        parts = ln.split()
        if not parts:
            continue
        m = rx.match(parts[0])
        if m:
            names.add(m.group(1))
    return names


# --------------------------------------------------------------------------- #
# Lifecycle locks. Every mutating pod path cooperates on these, including the
# per-pod env file writer above and :mod:`kiro_crew.pod.runtime_lifecycle`.
# --------------------------------------------------------------------------- #
_MUTEX_STATE = threading.local()


@contextlib.contextmanager
def pod_name_mutex(cfg: PodConfig, name: str):
    """Serialize this pod's lifecycle transactions per name, on every platform.

    ``down`` and ``up`` are independent entry points (the CLI, and Dev Fleet which
    shells out to it) with no other per-name coordination. Both platforms reclaim
    the isolated HOME on the ``down`` path, so both have the same race: a
    stop that has just confirmed the service gone races a concurrent start, whose
    checkout pin and service definition the stop's sweep would then delete. An
    exclusive flock on a sibling lock file makes each whole transaction (pin +
    definition + start on the up side; stop + drain + HOME sweep + env unlink on
    the down side) atomic with respect to the same name.

    **Reentrant within a thread** so the CLI can hold it across a transaction
    while :func:`start_pod` / :func:`stop_pod` re-acquire it internally (their own
    protection for direct callers): an advisory lock is per open-file-description,
    so a naive second acquisition in the same thread would deadlock against itself.

    Advisory and cooperative by design: every mutating path routes through here.
    The lock itself goes through :func:`kiro_crew.platform_compat.file_lock`, which
    is `flock` on POSIX and `msvcrt.locking` on Windows, so a pod plane on either
    platform is serialized by the same call. The fd comes from
    :func:`kiro_crew.platform_compat.open_lock_file`, which creates-or-opens
    without truncating: `open(path, "w")` truncates BEFORE any lock is held, and on
    Windows a contender then locks an already-emptied file. `file_lock` fails
    CLOSED, so a stuck holder raises instead of letting a second transaction run
    unserialized. The lock file is deliberately never deleted: unlinking one
    another process may be opening reintroduces the race the lock exists to close.
    """
    held = getattr(_MUTEX_STATE, "held", None)
    if held is None:
        held = _MUTEX_STATE.held = {}
    key = f"{cfg.unit_prefix}@{name}"
    if held.get(key, 0):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    lock_file = cfg.pods_dir / f"{key}.lock"
    with open_lock_file(lock_file) as fd:
        with file_lock(fd, exclusive=True):
            held[key] = 1
            try:
                yield
            finally:
                held[key] = 0


#: Reserved "name" the plane-wide lock borrows from :func:`pod_name_mutex`. Safe
#: because ``_NAME_RE`` forbids ``@``, so no real pod can ever produce this key.
_PLANE_LOCK_NAME = "@plane"


@contextlib.contextmanager
def pod_plane_mutex(cfg: PodConfig):
    """Serialize port CLAIMING across the whole pod plane, not just one name.

    :func:`pod_name_mutex` is per name, which is the right grain for the pin +
    definition + start transaction it guards -- two different pods have no reason
    to serialize their lifecycles. Port allocation is the exception: it is the one
    step where two DIFFERENT names contend, because they contend for the band
    rather than for each other's state.

    Without this, two colliding names ``up``'d concurrently (Dev Fleet's normal
    shape) hold disjoint name locks, both probe the same port free, and both boot
    onto it -- exactly the crash-loop :func:`allocate_port` exists to prevent.
    ``apps/backend_runtime/ports.py``'s ``_reserve_free_port`` carries the same lesson one
    subsystem over: "Probing without reserving ... lets two apps be handed the same
    port -- both children then bind it and the loser dies with EADDRINUSE."

    Implemented by BORROWING :func:`pod_name_mutex` under a reserved name rather
    than copying its body: the two differ only in the key, and a second
    hand-maintained copy would drift the moment either grew a feature. The key,
    lock file, reentrancy and lock ordering are therefore identical by
    construction rather than by review.

    **What this does NOT close, stated rather than implied.** The probe releases
    the port before the pod's gateway binds it, and the gateway is a separate
    process, so no lock held here can span the choose->bind gap; a unit is
    ``Type=simple``, so ``start_pod`` returns before the bind. Holding this until a
    health check confirmed the bind WOULD close it, at the cost of serializing
    every pod boot on the plane behind up to 45 health polls of the previous one.
    What closes most of the gap instead is :func:`_walk_band_for_free` consulting
    the pins already recorded on disk, so a concurrent claim is visible before its
    gateway is listening. See that function for the residue that remains.

    Held INSIDE :func:`pod_name_mutex` wherever both are taken, so the acquisition
    order is always name -> plane and cannot deadlock against a second holder.
    """
    with pod_name_mutex(cfg, _PLANE_LOCK_NAME):
        yield


# --------------------------------------------------------------------------- #
# Seed sanitization — deny-by-default. A seeded pod must NEVER be able to grab a
# live messaging identity, so we only ever return a config with the tunnel and
# every self-activating channel DISABLED. Anything that prevents us from positively
# guaranteeing that (missing file, bad JSON) returns None → the caller skips the
# seed and the pod boots blank.
# --------------------------------------------------------------------------- #

# Config sections a sanitized seed boots with ``enabled=False``. Deny-by-default:
# every channel carrying a config-level ``enabled`` is listed, because that flag is
# the only thing between a seed cloned from the real config (the intended
# ``--seed ~/.kiro/crew`` workflow) and a pod that answers real people as the
# operator's bot. The channel's credential is not a second gate — Telegram,
# Discord, Webex and Weixin read their token straight out of the seeded
# config.json; iMessage needs no credential at all, since its transport is the
# operator's own signed-in Messages.app; and Teams keeps its App ID in config.json
# while ``MICROSOFT_APP_PASSWORD`` reaches the pod through the inherited env.
# Slack is deliberately absent: it has no config-level enable, being gated purely
# on credentials that ``build_pod_env`` scrubs.
#
# ``test_pod.py`` pins this tuple against ``channels.builtin_channel_descriptors()``
# so a channel added to the roster cannot reach a pod ungated.
SEED_DISABLED_SECTIONS: tuple[str, ...] = (
    "tunnel",
    "wecom",
    "telegram",
    "discord",
    "webex",
    "teams",
    "weixin",
    "imessage",
    # WhatsApp is the sharpest case on this list: its transport is the operator's
    # OWN account, paired as a linked device, so a seeded pod booting it live would
    # send from the operator's real number using a credential that is not an env
    # var this env can scrub (the session store lives under the data home).
    "whatsapp",
    "feishu",
)


def _force_seed_agent_security(data: dict) -> None:
    """Keep a copied config from disabling the pod agent's isolation floor.

    A fixture or explicit seed directory is data, not authority to unconfine the
    agent that will inspect it. ``auto`` selects the platform backend and fails
    closed when none exists; both opt-outs are reset so a copied live config
    cannot turn that refusal into unsandboxed execution or suppress its warning.
    Other agent settings survive for realistic fixtures.
    """
    if not isinstance(data.get("agent"), dict):
        data["agent"] = {}
    agent = data["agent"]
    agent["sandbox"] = "auto"
    agent["sandbox_allow_unsandboxed_exec"] = False
    agent["sandbox_allow_no_isolation"] = False


def _apply_seed_config_floor(data: dict) -> None:
    """Disable self-activating sections and restore the agent sandbox floor."""
    for section in SEED_DISABLED_SECTIONS:
        if not isinstance(data.get(section), dict):
            data[section] = {}
        data[section]["enabled"] = False
    _force_seed_agent_security(data)


def sanitized_seed_config(seed_dir: Path) -> dict | None:
    """Read ``<seed_dir>/config.json`` and return it with ``enabled`` forced to
    False on every ``SEED_DISABLED_SECTIONS`` section, or None if it can't be
    safely sanitized (no file / bad JSON / sensitive path). Never copies any other
    state (DB / sessions / crons)."""
    # ``--seed`` is a user-supplied path: refuse to read from a sensitive /
    # credential location before touching the file. Resolve first so a symlink or
    # ".." can't smuggle past the guard.
    from kiro_crew.security import is_sensitive_path

    src_cfg = seed_dir / "config.json"
    if is_sensitive_path(os.path.realpath(str(src_cfg))):
        print(f"WARN: refusing to read seed config from sensitive path: {src_cfg} — skipping seed")
        return None
    if not src_cfg.is_file():
        return None
    try:
        data = json.loads(strip_utf8_bom(src_cfg.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        print("WARN: could not parse seed config.json — skipping seed (pod boots blank)")
        return None
    if not isinstance(data, dict):
        return None
    _apply_seed_config_floor(data)
    return data


def build_pod_env(
    cfg: PodConfig,
    home_dir: Path,
    port: int,
    checkout: Path,
    *,
    skip_model_download: bool = False,
) -> dict[str, str]:
    """Construct the isolated gateway environment for a pod.

    Scrubs messaging-identity creds so the pod can't inherit and re-use the live
    plane's Slack / WeCom / Telegram / Teams / Feishu identity via the systemd
    --user manager env: ``SLACK_*``, ``WECOM_*`` (WECOM_BOT_ID / WECOM_SECRET),
    ``MICROSOFT_APP_*``, ``FEISHU_*`` and non-AWS ``*_TOKEN`` (covers
    ``TELEGRAM_BOT_TOKEN``). Teams and Feishu each need their own prefix because
    none of ``MICROSOFT_APP_ID`` / ``MICROSOFT_APP_PASSWORD`` /
    ``MICROSOFT_APP_TENANT_ID`` / ``FEISHU_APP_ID`` / ``FEISHU_APP_SECRET`` ends
    in ``_TOKEN``, so the generic suffix rule that catches every other channel's
    bot credential passes the Azure Bot secret and the Feishu app secret straight
    through. The loader's complete credential roster is then scrubbed except for
    ``KIRO_API_KEY`` (the pod agent's model credential) and ``KIROCREW_OWNER_ID``
    (dashboard ownership, not a channel or source-provider identity). Provider CLI
    config roots are redirected beneath the pod home so ``gh``, ``glab`` and ``az``
    cannot reuse the operator's persisted login sessions through the deliberately
    inherited real ``HOME``. ``AWS_*`` is kept on purpose (pods run agent turns),
    and the generic ``_TOKEN`` scrub deliberately excludes ``AWS_`` so
    ``AWS_SESSION_TOKEN`` survives intact. Config-level channel enables are
    additionally forced off by ``sanitized_seed_config`` (defense-in-depth).

    ``KIROCREW_OS_HOME`` points the pod's own :mod:`kiro_crew.mcp_grant` reads
    (mint, status, disconnect, mcp_discovery's remote probe -- all resolved
    through ``config.paths.kiro_oauth_cache_home``) at a dedicated
    ``<home_dir>/os-home`` tree INSTEAD of the real host home. Without this a
    pod's gateway process stats and unlinks MCP OAuth grant artifacts under the
    REAL ``~/.aws/sso/cache`` -- so a Connections card in the pod reads
    "Connected" from a grant the operator minted on the real machine, and a
    grant minted inside the pod is a real, durable machine-level credential
    that OUTLIVES ``pod down``. This directory is nested INSIDE ``home_dir`` so
    ``cleanup_home``'s teardown reclaims it with everything else. It holds no
    secret by itself -- see ``_seed_pod_os_home`` for what is staged into it,
    and ``acp/client.py`` / ``acp/runtime.py`` for the matching ``HOME`` remap
    on the pod's OWN kiro-cli children, which is what makes kiro-cli's writes
    land in this same tree.

    ``skip_model_download`` boots the pod in the documented no-embedding-model
    mode by exporting ``KIROCREW_SKIP_MODEL_DOWNLOAD=1`` into THIS env only. It is
    a value in the returned mapping, never a write to the operator's shell,
    profile or real data home -- the whole point is that a load test can run
    embedding-free without the host losing its own model. The pod then serves
    memory and knowledge search through the keyword fallback, which is a
    supported mode rather than a broken one, so the instance stays usable.

    Deliberately the existing skip-download switch rather than a
    ``KIROCREW_EMBED_MODEL_URL`` pointed at an unreachable host. That spelling
    reaches the same end state only after the downloader has spent its full
    attempt budget on HTTPS requests to a host chosen to fail, and it carries a
    live footgun: :func:`kiro_crew.embeddings._resolve_model_url` IGNORES any
    override that is not ``https://`` and falls back to the real CDN, so one
    malformed sentinel downloads the very model the option exists to avoid.
    """
    os_home = home_dir / "os-home"
    env = {
        **os.environ,
        "HOME": os.environ.get("HOME", str(Path.home())),
        "GH_CONFIG_DIR": str(home_dir / ".config" / "gh"),
        "GLAB_CONFIG_DIR": str(home_dir / ".config" / "glab-cli"),
        "AZURE_CONFIG_DIR": str(home_dir / ".azure"),
        "AZURE_EXTENSION_DIR": str(home_dir / ".azure" / "cliextensions"),
        "KIROCREW_HOME": str(home_dir),
        "KIROCREW_OS_HOME": str(os_home),
        "KIROCREW_PORT": str(port),
        "KIROCREW_PROJECT_DIR": str(checkout),
        # Declare pod identity. A pod is ephemeral by construction — `pod down`
        # deletes this home and the checkout venv — so the agent-spec write guard
        # keys on THIS marker rather than on "has an isolated KIROCREW_HOME",
        # which would also catch a CI test gateway or a user who simply relocated
        # their data home, and wrongly stop both from writing their own specs.
        "KIROCREW_POD": "1",
        # The pod's OWN kiro user home, so its agent specs, prompts, skills and
        # chat transcripts all live under the pod instead of the machine-wide
        # ``~/.kiro``. This is what stops a pod boot from rewriting the real
        # install's specs -- and, just as importantly, stops a pod that was merely
        # BLOCKED from rewriting them falling back to the shared spec, whose env
        # pins the LIVE data home (so a pod's ``learn_add`` would have written the
        # real lessons). Safe only because every KiroCrew reader of the transcripts
        # dir now resolves through ``kiro_sessions_dir()``; without that the pod
        # would write sessions somewhere KiroCrew never looks and lose resume.
        # Inside the pod HOME so ``pod down``'s teardown reclaims it.
        "KIRO_HOME": str(home_dir / "kiro"),
        # Give the pod its OWN workspace root. Without this, `workspace_root()`
        # finds no `KIROCREW_WORKSPACE` and no `config_dir()/workspace_dir` file in
        # a fresh pod home, so it falls through to the platform default under the
        # REAL `HOME` — and every agent turn in the pod (a `chat`/`run`/`tui`
        # through `pod exec`, and the pod gateway's own sessions) would read and
        # WRITE the live workspace. `KIROCREW_WORKSPACE` is the documented override
        # (config/loader.py:220, used as-is) and `eval/runner.py` already scopes a
        # run the same way, so this is the existing mechanism rather than new
        # resolution behaviour. Placing it inside the pod HOME means `pod down`'s
        # teardown removes it with everything else.
        "KIROCREW_WORKSPACE": str(home_dir / "workspace"),
        # Pin the bind address to the SAME loopback every pod-plane client dials.
        # `health()` and `mint_token()` connect to `http://127.0.0.1:<port>`, and
        # the ownership attestation vouches for
        # "the process our sidecar names" — never for which ADDRESS that process
        # bound. An inherited KIROCREW_BIND (the official image exports
        # `0.0.0.0`; a user shell can carry `::1`) flows through the systemd
        # --user manager env into the pod gateway, which reads it in
        # dashboard/urls.py. On `::1` the gateway would serve the IPv6 loopback
        # while 127.0.0.1:<port> stays free for a foreign local process to bind
        # — a fresh, truthful attestation would then authorize a mint whose HTTP
        # request lands on the foreigner. Pinning collapses the listener and the
        # attestation onto one address; it also stops a stray `0.0.0.0` from
        # exposing a pod beyond the host.
        #
        # `pod_api()` is deliberately NOT in that list: it spells the same URL,
        # but only so the gateway's Host check sees what it would on TCP — the
        # connection itself goes over the pod's unix socket, which no bind address
        # can redirect. It needs no pin, and that is the point.
        "KIROCREW_BIND": "127.0.0.1",
        # The pod's OWN venv leads PATH, ahead of cfg.gateway_path (which starts
        # with ~/.local/bin). Without this a bare `kirocrew` inside a pod — an
        # agent bash turn, a subprocess, `_kirocrew_bin()`'s "console-script on
        # PATH" probe — resolves the machine-wide shim instead of the checkout
        # under test, so the pod silently exercises the global install and stays
        # coupled to a symlink it does not own.
        "PATH": os.pathsep.join([str(prov.venv_bin_dir(checkout)), cfg.gateway_path]),
    }
    for key in [
        k
        for k in env
        if k.startswith("SLACK_")
        or k.startswith("WECOM_")
        or k.startswith("MICROSOFT_APP_")
        or k.startswith("FEISHU_")
        or k.startswith("JIRA_TOKEN_")
        or (k.endswith("_TOKEN") and not k.startswith("AWS_"))
    ]:
        env.pop(key, None)
    from kiro_crew.config.loader import CRED_KIRO_API_KEY, CRED_OWNER_ID, CREDENTIAL_KEYS

    for key in set(CREDENTIAL_KEYS) - {CRED_KIRO_API_KEY, CRED_OWNER_ID}:
        env.pop(key, None)
    # Cross-plane guard: a gateway-descended caller inherits the LIVE
    # gateway's KIROCREW_BOUND_PORT (dashboard.server._export_bound_port).
    # Inside a pod env it would name the wrong plane — the pod's own
    # KIROCREW_PORT above is the target — so drop it unconditionally rather
    # than rely on resolution precedence alone.
    env.pop("KIROCREW_BOUND_PORT", None)
    # Same cross-plane guard for the companion host evidence (exported only
    # for specific-interface binds — see dashboard.server).
    env.pop("KIROCREW_BOUND_HOST", None)
    if skip_model_download:
        # Set AFTER the scrub loop so no present-or-future scrub pattern can strip
        # the guarantee back out.
        env[SKIP_MODEL_DOWNLOAD_ENV] = "1"
        # The switch gates only the DOWNLOAD (embeddings.ensure_model). An inherited
        # KIROCREW_EMBED_MODEL_PATH is read FIRST by resolve_custom_model, so with it
        # in place the pod would load the operator's custom GGUF and embed while the
        # journal says it does not -- a load measurement wrong in exactly the
        # direction this option exists to control. Drop every embed-model override
        # with the switch so the returned mapping describes one mode. The URL
        # override is moot once nothing downloads; it goes too so the mapping cannot
        # say two things at once.
        for key in EMBED_MODEL_OVERRIDE_ENVS:
            env.pop(key, None)
    return env


def _close_fd(fd: int) -> None:
    """Close *fd*, ignoring an already-closed descriptor."""
    try:
        os.close(fd)
    except OSError:
        pass


def _ensure_pod_dir(target: Path, *, what: str) -> None:
    """Create-if-absent *target* as an owner-only directory, refusing planted links.

    THE directory half of the boot path's write hardening, and the companion to
    :func:`pinned_fs.write_file_pinned`. Returns nothing on purpose: an earlier
    revision handed the caller a raw descriptor to ``fchmod``, which made the mode
    the caller's problem and does not exist on the platform where ``fchmod`` is a
    no-op. The mode is applied here, through the descriptor where there is one.

    The ANCESTOR chain is created by name first, deliberately: ``pinned_fs``
    creates only the final component, and the chain above a pod home is the pod
    ROOT (``~/.kiro/crew/pods`` by default), which is host-owned state this module
    already creates by name in two other places. What is agent-influenced is the
    pod's own directory and everything under it, and that is what gets pinned.

    Platform split matches :func:`pinned_fs.write_file_pinned` exactly -- pinned
    create plus ``fchmod`` where :func:`pinned_fs.supports_pinned_walk` holds, and
    elsewhere the ``lstat`` link refusal (the everywhere-floor) plus a by-name
    ``mkdir``. Raises :class:`PodError` on a planted link or an unusable component;
    ``boot`` converts that into a recorded terminal refusal.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PodError(f"could not create the parent of {what} {target}: {exc}") from exc
    if not pinned_fs.supports_pinned_walk():
        existing = pinned_fs.lstat_by_name(target)
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise PodError(f"refusing to use {what} {target}: it is a symbolic link")
        try:
            target.mkdir(mode=0o700, exist_ok=True)
            os.chmod(target, stat.S_IRWXU)
        except OSError as exc:
            raise PodError(f"could not prepare {what} {target}: {exc}") from exc
        return
    fd = pinned_fs.create_and_open_dir_pinned(target, what=what, refusal=PodError)
    try:
        os.fchmod(fd, stat.S_IRWXU)  # mkdir's mode is umask-masked; this is not
    except OSError as exc:
        raise PodError(f"could not tighten {what} {target}: {exc}") from exc
    finally:
        _close_fd(fd)


# --------------------------------------------------------------------------- #
# Compatibility facade. A re-exported name is ABSENT from this module's own
# namespace on purpose: ``__getattr__`` runs only for a name the module does not
# hold, so a binding here would shadow the owner for every later read, and a
# patch of ``rt.<name>`` would reach nothing the owner's code reads.
# --------------------------------------------------------------------------- #
#: Owner module -> every name this module re-exports from it: the names the owner
#: defines, plus the imports that moved there with their reader, so each name keeps
#: resolving here (a patch of ``rt.find_port_listeners`` has to land where
#: ``port_owner`` reads it). A re-exported MODULE resolves for its attributes only.
_EXPORTS_BY_OWNER: dict[str, tuple[str, ...]] = {
    "kiro_crew.pod.runtime_ports": (
        "AUTO_PORT_KEY",
        "_MAX_PEER_ENV_BYTES",
        "_MAX_PORT_DIGITS",
        "_peer_effective_port",
        "_pinned_port",
        "_port_from_env",
        "_port_is_free",
        "_ports_claimed_by_other_pods",
        "_posix_cksum",
        "_read_peer_env",
        "_walk_band_for_free",
        "allocate_port",
        "derive_port",
        "operator_pinned",
        "socket",
    ),
    "kiro_crew.pod.runtime_attestation": (
        "OWNER_FOREIGN",
        "OWNER_POD",
        "OWNER_UNPROVEN",
        "_pod_pid_record_path",
        "_pod_recorded_pid",
        "_unproven_remedy",
        "attributed_descendants",
        "find_port_listeners",
        "listening_pid_tool_available",
        "loopback_owner_pids",
        "port_owner",
        "process_start_time",
        "run_marker",
    ),
    "kiro_crew.pod.runtime_client": (
        "API_BODY_MAX_BYTES",
        "API_METHODS",
        "API_READ_METHODS",
        "API_TIMEOUT_SECS",
        "HEALTH_FOREIGN",
        "_MAX_ECHOED_DETAIL_LEN",
        "_MINT_403_BODY_CAP",
        "_POD_RECREATE",
        "_attested_gateway_verifier",
        "_authenticated_url",
        "_mint_403_cause",
        "_pod_mint_secret",
        "_pod_secret_candidates",
        "_pod_secret_path",
        "_probe_health",
        "_read_capped",
        "_read_pod_secret_file",
        "_scrub_json_tokens",
        "_scrub_token",
        "_scrub_token_string",
        "_terminal_safe_detail",
        "api_path",
        "get_peer_pid",
        "health",
        "http",
        "loopback_urlopen",
        "mint_token",
        "pod_api",
        "published_credential",
        "unix_socket_urlopen",
    ),
    "kiro_crew.pod.runtime_home": (
        "SeedError",
        "StoreMapping",
        "_CREATE_ATTEMPTS",
        "_HOME_RECLAIM_ATTEMPTS",
        "_HOME_RECLAIM_PAUSE_SECS",
        "_RUNTIME_AUTH_STORE_FILE_CAP",
        "_SQLITE_SIDECAR_SUFFIXES",
        "_SQLITE_SUFFIXES",
        "_fixture_name_from_manifest_text",
        "_is_sqlite_sidecar",
        "_open_seed_regular_file",
        "_pin_created_dir_windows",
        "_pin_outermost_existing_windows",
        "_prepare_seeded_home_dir",
        "_prepare_seeded_home_fd",
        "_refuse_reparse_chain",
        "_rmtree_bounded",
        "_runtime_auth_store_mappings",
        "_seed_home_windows",
        "_seed_pod_os_home",
        "_seed_pod_os_home_windows",
        "_seeded_scenario_from_fd",
        "_seeded_scenario_in_dir",
        "_snapshot_sqlite_pinned",
        "_stage_runtime_auth_store",
        "_stage_runtime_auth_store_windows",
        "_surviving_entries",
        "atomic_write_at",
        "cleanup_home",
        "is_link_or_junction",
        "is_scenario_ref",
        "open_file_no_reparse",
        "orphan_homes",
        "pin_directory",
        "resolve_seed_scenario",
        "resolved_pod_home",
        "seed_home_from_scenario",
        "seed_mod",
        "seeded_scenario_in_home",
        "store_mappings",
        "write_pod_config",
    ),
    "kiro_crew.pod.runtime_lifecycle": (
        "DRAIN_TIMEOUT_SECS",
        "RECLAIMED_MARKER",
        "_CGROUP_ROOT",
        "_install_pod_dropin",
        "_refresh_stale_unit",
        "_stop_pod_launchd",
        "_stop_pod_windows",
        "_write_and_load_unit",
        "cgroup_procs_file",
        "drain_cgroup",
        "halt_pod",
        "install_backend",
        "loaded_teardown_hook",
        "start_pod",
        "stop_pod",
        "time",
        "unit_mod",
    ),
    "kiro_crew.pod.runtime_boot": (
        "EXIT_PROVISIONING",
        "EXIT_REFUSED_UNRECOVERABLE",
        "TERMINAL_BOOT_EXIT_CODES",
        "_CHILD_VIABILITY_TIMEOUT_SECS",
        "_POD_EQUIVALENT",
        "_POD_SAFE_VERBS",
        "_boot_unguarded",
        "_clear_refusal",
        "_probe_pod_child_bootstrap",
        "_record_refusal",
        "_refuse",
        "boot",
        "exec_in_pod",
        "pod_context",
        "refusal_reason",
        "require_pod_safe_verb",
        "target_supports_flag",
        "terminal_exit_code",
    ),
}


def _index_exports() -> dict[str, str]:
    """Invert :data:`_EXPORTS_BY_OWNER`, refusing a name with two homes."""
    index: dict[str, str] = {}
    for module_name, names in _EXPORTS_BY_OWNER.items():
        for name in names:
            if name in index or name in globals():
                raise RuntimeError(f"pod runtime name {name!r} has two owners")
            index[name] = module_name
    return index


#: Re-exported name -> the dotted NAME of its owner, never the module object: the
#: owner is read from :data:`sys.modules` on each use, so a module purged and
#: imported again is seen at once instead of this table forwarding to the old copy.
_EXPORTS: dict[str, str] = _index_exports()


def _owner(name: str) -> ModuleType:
    """Return the module that owns re-exported *name*, resolved on each access.

    ``importlib.import_module`` is the resolution rather than a mapping kept here.
    It answers from :data:`sys.modules`, the one place a module is stored, so a
    purged or replaced owner is seen at once; and it waits on that module's import
    lock while its body is still running, where a bare ``sys.modules`` read would
    hand a thread a half-built owner another thread is still importing.
    """
    return importlib.import_module(_EXPORTS[name])


# Hidden from type checkers: mypy types every unknown attribute of a module that
# defines ``__getattr__`` as ``Any``, so a mistyped or removed ``rt.<name>`` would
# type-check. mypy sees every public re-export through the ``TYPE_CHECKING`` imports.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a re-exported name from the module that owns it (:pep:`562`)."""
        if name not in _EXPORTS:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


def _refuse_module_rebind(name: str, current: object) -> None:
    """Refuse to rebind or delete a MODULE through this namespace, loudly.

    Every pod runtime module imports its own binding of the modules it uses, so
    replacing ``rt.time`` or ``rt.launchd`` here would reach one reader and leave the
    others on the real module -- a patch that silently misses. Its attributes are
    shared by every reader, so that is what a test patches.
    """
    label = getattr(current, "__name__", name)
    raise AttributeError(
        f"{__name__}.{name} is the shared module {label!r}; rebinding it here "
        f"would reach only one pod runtime module. Patch its attributes instead."
    )


class _ReExportModule(ModuleType):
    """Send a write or delete of a re-exported name to the module that owns it.

    Binding it here instead would shadow the owner for every later read, because
    ``__getattr__`` runs only for a name this module does not hold. Forwarded, a
    ``monkeypatch`` or ``mock.patch`` round-trips: ``mock.patch`` restores a name
    this module does not hold by deleting it and setting it back. With
    ``create=True`` it skips the set, which would leave the owner without the name,
    so ``test/test_pod_runtime_refactor_create_guard.py`` fails on any such patch.

    A name in :data:`_MODULE_NAMES` is refused both ways, by name: writing anything
    but the module it already holds, or deleting it. Any other name takes any value,
    a module included, and gives it back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _MODULE_NAMES and value is not getattr(self, name, None):
            _refuse_module_rebind(name, getattr(self, name, None))
        if name in _EXPORTS:
            setattr(_owner(name), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _MODULE_NAMES:
            _refuse_module_rebind(name, getattr(self, name, None))
        if name in _EXPORTS:
            delattr(_owner(name), name)
        else:
            super().__delattr__(name)


# Every owner is imported here, once this module has bound all of its own names: an
# owner reads the core as ``runtime.<name>``, so it cannot load before them. Loading
# them now rather than on first use takes each owner's module-level bindings --
# ``from kiro_crew.platform_compat import pin_directory`` among them -- when this
# module is imported, as they were when the runtime was one module. On first use,
# that moment could fall inside a test's patch of the source module, and the owner
# would keep the patched value for the rest of the process.
for _module_name in _EXPORTS_BY_OWNER:
    importlib.import_module(_module_name)
del _module_name

#: The names, here or on an owner, that are bound to a MODULE once every owner has
#: loaded. Fixed by name rather than judged by the value a name holds at the moment
#: of a write, so a function patched with a module stub is still undone, and a
#: module name stays refused whatever it was last set to.
_MODULE_NAMES = frozenset(
    name
    for name, value in [
        *globals().items(),
        *((name, getattr(_owner(name), name)) for name in _EXPORTS),
    ]
    if isinstance(value, ModuleType) and not name.startswith("__")
)

# Installed once this module's own names are bound and its owners have loaded, so
# the forwarding is live for every caller but never runs during either.
sys.modules[__name__].__class__ = _ReExportModule

# ``from kiro_crew.pod.runtime import *`` consults this list and never reaches
# ``__getattr__``, so without it a star import would carry only the names this
# module binds itself. It is DERIVED from the two authorities -- what this module
# binds and the re-export table -- so it is not a third list to keep in step.
__all__ = sorted(name for name in set(globals()) | set(_EXPORTS) if not name.startswith("_"))

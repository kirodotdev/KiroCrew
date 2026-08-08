"""Pod runtime mechanics: git worktree resolution, port derivation, systemd
wrappers, boot, token mint.

Everything that talks to the host (``git``, ``systemctl --user``, ``cksum``, the
pod's ``.local_secret``) lives here so :mod:`kiro_crew.pod.cli` stays a thin verb
layer. No state is held; each function reads what it needs from a
:class:`PodConfig` (and, for worktree resolution, from git / the per-pod env file).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.platform_compat import IS_LINUX, IS_MACOS
from kiro_crew.pod import launchd
from kiro_crew.pod import provision as prov
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import PodConfig

# Pod names become systemd instance names and path segments; keep them strict.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")


class PodError(RuntimeError):
    """A pod operation could not be completed (bad name, no worktree, mint failed…)."""


def validate_name(name: str) -> str:
    if not name or not _NAME_RE.match(name):
        raise PodError(f"invalid pod name {name!r}")
    return name


# --------------------------------------------------------------------------- #
# Per-pod env file (pinned CHECKOUT= / PORT= / SEED= / APPROVAL=). Values are
# single-quoted on write and unquoted on read; unknown keys are preserved on
# merge.
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


def read_env_file(cfg: PodConfig, name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    f = cfg.env_file(name)
    try:
        if f.exists():
            for ln in f.read_text().splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                key, val = ln.split("=", 1)
                raw = val.strip()
                # Strip a single matched surrounding quote pair only (the form
                # write_env_file emits), so a value that legitimately contains a
                # quote is not mangled.
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                    raw = raw[1:-1]
                out[key.strip()] = raw
    except OSError:
        pass
    return out


def write_env_file(cfg: PodConfig, name: str, updates: dict[str, str]) -> None:
    """Merge *updates* into the pod's env file, preserving existing keys.

    Values MUST be single-line: the ``KEY='value'`` format does not escape
    newlines, so a multi-line value would not round-trip. ``--seed`` is
    user-supplied, so reject a newline-bearing value loudly (fail-closed) rather
    than silently writing an un-parseable file.
    """
    data = read_env_file(cfg, name)
    data.update(updates)
    for key, val in data.items():
        if "\n" in val or "\r" in val:
            raise PodError(f"pod env value for {key!r} must be single-line")
    cfg.pods_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}='{v}'\n" for k, v in data.items())
    cfg.env_file(name).write_text(body)


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
            text=True,
            timeout=10,
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


# --------------------------------------------------------------------------- #
# Port derivation. POSIX ``cksum`` is a specific CRC that is NOT zlib.crc32, so
# we shell the same binary rather than reimplement it and risk drifting the port.
# --------------------------------------------------------------------------- #
def _pinned_port(cfg: PodConfig, name: str) -> int | None:
    """A ``PORT=`` pinned in the pod's env file wins over derivation."""
    val = read_env_file(cfg, name).get("PORT")
    if val and val.isdigit():
        return int(val)
    return None


def derive_port(cfg: PodConfig, name: str) -> int:
    """Resolve pod *name*'s port: pinned ``PORT=`` else ``base + (cksum % 199) + 1``."""
    pinned = _pinned_port(cfg, name)
    if pinned is not None:
        return pinned
    try:
        out = subprocess.run(["cksum"], input=name, capture_output=True, text=True, timeout=5)
        cks = int(out.stdout.split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        raise PodError(f"cksum failed for {name!r}: {exc}") from exc
    return cfg.base_port + (cks % 199) + 1


def pod_unit(cfg: PodConfig, name: str) -> str:
    """systemd unit name for pod *name*."""
    return f"{cfg.unit_prefix}@{name}.service"


def pod_home(cfg: PodConfig, name: str) -> Path:
    return cfg.home_dir(name)


# --------------------------------------------------------------------------- #
# systemd --user helpers.
# --------------------------------------------------------------------------- #
def _session_runtime_dir() -> str:
    """The per-user runtime directory ``systemctl --user`` resolves against.

    ``XDG_RUNTIME_DIR`` when the caller has one, else systemd's conventional
    ``/run/user/<uid>``. ``os.getuid`` is absent on Windows; pods are Linux-only
    (:func:`require_systemd`) so that branch is unreachable at runtime, but the
    ``getattr`` keeps this module importable there.
    """
    explicit = os.environ.get("XDG_RUNTIME_DIR")
    if explicit:
        return explicit
    uid = getattr(os, "getuid", lambda: -1)()
    return f"/run/user/{uid}"


def session_bus_socket() -> str:
    """Path of the D-Bus socket that fronts this user's systemd instance."""
    return os.path.join(_session_runtime_dir(), "bus")


def has_session_bus() -> bool:
    """Whether ``systemctl --user`` can reach a per-user systemd instance.

    An explicitly-set ``DBUS_SESSION_BUS_ADDRESS`` is taken at face value (the
    caller has deliberately pointed somewhere, possibly not a filesystem path);
    otherwise the conventional socket must actually exist.
    """
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    return os.path.exists(session_bus_socket())


def _systemctl_env() -> dict[str, str]:
    """Environment for ``systemctl --user``, with the session-bus pointers
    backfilled when absent.

    ``systemctl --user`` finds the per-user systemd instance through
    ``XDG_RUNTIME_DIR`` + ``DBUS_SESSION_BUS_ADDRESS``. A process launched from
    a systemd SYSTEM unit — which is how ``kirocrew service install`` runs the
    gateway — inherits no login-session environment and therefore neither
    variable, so every pod verb died with "Failed to connect to bus: No medium
    found" even though the bus socket was present and the pod unit installed.

    Only ever ADDS: an explicitly-set value always wins, so a caller that has
    deliberately pointed at another bus is left untouched. The socket must
    exist before we name it — if ``systemd --user`` genuinely is not running we
    want systemctl's own diagnostic, not a failure against a path we invented.
    """
    env = {**os.environ}
    runtime_dir = _session_runtime_dir()
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        sock = os.path.join(runtime_dir, "bus")
        if os.path.exists(sock):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={sock}"
    env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    return env


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_systemctl_env()
    )


def require_systemd() -> None:
    """Raise :class:`PodError` unless this host can run ``systemctl --user``.

    Pods are Linux ``systemd --user`` only (see ``pod/README.md`` → Platform).
    Without this gate the first ``subprocess.run(["systemctl", ...])`` raises a
    bare ``FileNotFoundError`` and every verb dumps a traceback on macOS /
    Windows instead of the documented "report the failure" one-liner. Checked
    here — the single chokepoint every systemd call funnels through — so no verb
    can forget it.

    The third gate is the session bus. :func:`_systemctl_env` backfills the bus
    pointers when the socket exists, but when ``systemd --user`` is genuinely
    not running (no login session and ``Linger=no``) there is nothing to point
    at and systemctl emits a raw "Failed to connect to bus: No medium found"
    that names neither the cause nor the fix. Translate it here, keyed on the
    socket's absence rather than on matching systemctl's stderr.
    """
    if not IS_LINUX:
        raise PodError(
            f"pods require Linux `systemctl --user`; this host is {sys.platform}. "
            "Use `./dev-backend.sh` to preview a worktree on this platform."
        )
    if shutil.which("systemctl") is None:
        raise PodError(
            "pods require `systemctl --user`, but no `systemctl` was found on PATH."
        )
    if not has_session_bus():
        uid = getattr(os, "getuid", lambda: -1)()
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(uid)
        raise PodError(
            f"no `systemd --user` session bus for uid {uid} "
            f"(looked for {session_bus_socket()}).\n"
            "Pods are systemd --user units, so one is required.\n"
            f"Fix: loginctl enable-linger {user}   "
            "# keeps the per-user instance alive independently of login sessions"
        )


def require_backend() -> None:
    """Gate on whatever service manager THIS host uses for pods.

    Dispatches instead of replacing :func:`require_systemd`: that function is
    still the systemd gate with its own contract and messages, so Linux and
    Windows behaviour is provably unchanged by the macOS work — on any non-darwin
    host this is exactly ``require_systemd()``.
    """
    if IS_MACOS:
        try:
            launchd.require_backend()
        except launchd.LaunchdError as exc:  # translate to the pod error type
            raise PodError(str(exc)) from exc
        return
    require_systemd()


def systemctl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    require_systemd()
    return _run(["systemctl", "--user", *args], timeout=timeout)


def is_active(cfg: PodConfig, name: str) -> bool:
    if IS_MACOS:
        try:
            return launchd.is_active(cfg, name)
        except launchd.LaunchdError as exc:
            # Fail closed as the documented pod error, not a traceback: the
            # probe REFUSES to call a pod absent when launchctl cannot answer.
            raise PodError(str(exc)) from exc
    cp = systemctl("is-active", "--quiet", pod_unit(cfg, name))
    return cp.returncode == 0


def unit_state(cfg: PodConfig, name: str) -> tuple[str, int]:
    """(ActiveState, NRestarts) for the pod's unit — ("unknown", 0) on error.

    Lets the up-path tell a CRASHED/crash-looping worktree gateway (a broken
    build, import error, bad config) apart from one that is just slow to come up —
    so we fail fast with the gateway's own error instead of polling a dead unit
    for the full timeout.

    On macOS launchd exposes no restart counter; see
    :func:`kiro_crew.pod.launchd.unit_state` for how the crash signal is
    preserved without one.
    """
    if IS_MACOS:
        return launchd.unit_state(cfg, name)
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
    """Tail the pod's log — used to surface a boot failure's real cause.

    launchd has no journal, so on macOS this tails the files the pod's plist
    routes stdout/stderr to. Same contract, different mechanism.
    """
    if IS_MACOS:
        return launchd.recent_journal(cfg, name, lines=lines)
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
# Sentinel in stop_pod's stdout meaning the pod NAME was reclaimed by a new pod
# mid-teardown (down/up race). The old pod is gone, but per-name state (the env
# file pinning CHECKOUT=) now belongs to the NEW pod and must not be deleted.
RECLAIMED_MARKER = "pod-name-reclaimed-by-new-pod"


# Backend-agnostic lifecycle. The CLI calls these so no verb has to know which
# service manager it is talking to — and so the launchd-only teardown obligation
# (below) cannot be forgotten at one call site and honoured at another.
# --------------------------------------------------------------------------- #
def pod_name_mutex(cfg: PodConfig, name: str):
    """Per-name lifecycle mutex for callers composing multi-step transactions.

    The CLI wraps `up` (pin -> plist -> bootstrap) and `down` (stop -> sweep ->
    env unlink) in this so per-name METADATA moves atomically with the service
    operations — the review-blocking down/up races all lived in those seams.
    Reentrant with the acquisition inside start_pod/stop_pod. On Linux the unit
    (ExecStopPost) owns teardown ordering, so this is a no-op there.
    """
    if IS_MACOS:
        return launchd.pod_mutex(cfg, name)
    return contextlib.nullcontext()


def start_pod(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Bring pod *name* up through whichever service manager this host uses."""
    if IS_MACOS:
        # Re-rendered every start, which is why launchd needs no equivalent of
        # the systemd path's stale-ExecStart self-heal. The mutex serializes
        # against a concurrent stop of the same name, whose plist unlink and
        # HOME sweep would otherwise race this write (see launchd.pod_mutex).
        with launchd.pod_mutex(cfg, name):
            launchd.write_plist(cfg, name)
            return launchd.start(cfg, name)

    # Self-heal a dangling ExecStart binary: the template unit bakes an absolute
    # kirocrew path at install time; if the worktree it resolved into was pruned
    # since, every start fails EXEC (203).
    if not unit_mod.unit_exec_ok(cfg):
        unit_mod.install_unit(cfg)
        rel = systemctl("daemon-reload")
        if rel.returncode != 0:
            return rel
    return systemctl("start", pod_unit(cfg, name))


def stop_pod(cfg: PodConfig, name: str) -> subprocess.CompletedProcess:
    """Stop pod *name* and guarantee its isolated HOME is gone.

    On Linux the unit's ``ExecStopPost`` runs ``pod _cleanup``, so teardown
    happens whatever stopped the service. launchd has **no post-stop hook**, so
    the HOME removal is done here after a successful ``bootout`` — routed through
    :func:`cleanup_home`, which re-validates the name, rather than a raw
    recursive delete. Both paths therefore end with zero residue; only the macOS
    one depends on this call being reached, which is why the crash case is
    covered separately by ``launchd.orphan_homes()``.
    """
    if IS_MACOS:
        # One mutex across bootout + confirmation + the HOME sweep: a
        # concurrent `up` of the same name blocks until teardown fully
        # finishes instead of interleaving with it (the plist-presence
        # reclaim check below stays as defense-in-depth — the flock is
        # advisory and only guards paths that route through it).
        with launchd.pod_mutex(cfg, name):
            # launchd.stop() is authoritative: rc 0 means the label is confirmed
            # unloaded (a bootout of an unloaded label is a no-op success). A non-zero
            # rc means the unload could NOT be confirmed — in that case do NOT touch
            # the HOME: it may belong to a live gateway, and deleting it while
            # returning success was exactly the blocking review finding here.
            cp = launchd.stop(cfg, name)
            if cp.returncode != 0:
                return cp
            # Reap with a short grace window. launchd confirms the SERVICE process is
            # unloaded, but a dying child can outlive it by a beat and flush state on
            # exit — observed in a real teardown: cleanup ran, verification passed,
            # then a child wrote settings back and resurrected the HOME milliseconds
            # later. Retry the reap a few times so a final write cannot win the race;
            # if the directory still reappears after the window, someone is genuinely
            # alive in there and we must say so, not shrug.
            leftover = pod_home(cfg, name)
            # Observe the FULL window — no early exit on a clean sample (the dying
            # child that motivated this flushed state after a beat). But DO exit the
            # moment the name is claimed by a NEW pod: `down` and `up` are
            # independent Dev Fleet endpoints with no per-name lock, so a quick
            # down→up would otherwise put the fresh gateway's HOME under the
            # remaining rmtree passes. Our unload was already confirmed, and a new
            # `up` re-writes the plist BEFORE bootstrapping — so plist presence is
            # the claim marker. Deliberately a pure filesystem check: probing
            # launchctl here would shell out on every sweep and break on hosts
            # without launchd (the unit suites run this path on Linux/Windows CI).
            #
            # A reclaimed name is reported via RECLAIMED_MARKER in stdout so the
            # caller knows the teardown handed over: it must NOT delete the per-pod
            # env file, which now pins the NEW pod's checkout.
            for _ in range(6):
                if launchd.plist_path(cfg, name).exists():
                    return subprocess.CompletedProcess(
                        args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
                    )
                cleanup_home(cfg, name)
                time.sleep(0.5)
            if launchd.plist_path(cfg, name).exists():
                return subprocess.CompletedProcess(
                    args=[], returncode=0, stdout=RECLAIMED_MARKER, stderr=""
                )
            cleanup_home(cfg, name)
            if leftover.exists():
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout=cp.stdout or "",
                    stderr=(
                        f"pod stopped but its isolated HOME keeps reappearing at "
                        f"{leftover} — a process is still writing there, so teardown "
                        "is incomplete. Remove it by hand and report this."
                    ),
                )
            return subprocess.CompletedProcess(args=[], returncode=0, stdout=cp.stdout or "", stderr="")
    return systemctl("stop", pod_unit(cfg, name))


def install_backend(cfg: PodConfig) -> tuple[str, subprocess.CompletedProcess | None]:
    """Install whatever machine-wide definition the backend needs.

    systemd needs one template unit + a daemon-reload. launchd has no template
    concept — each pod's plist is written at ``up`` — so there is nothing to
    install, and saying so is better than writing a file that does nothing.

    Returns ``(message, reload_result)``. Raises :class:`PodError` only for an
    unusable host, and does so BEFORE writing anything, so an unsupported
    platform never leaves a stray definition behind. A failed reload comes back
    as the second element rather than an exception, because the caller reports
    that as a hard exit while the gate refusal is converted by the CLI's
    dispatch layer — two different documented behaviours.
    """
    require_backend()
    if IS_MACOS:
        return (
            "nothing to install on macOS: launchd has no template units, so each "
            "pod's agent plist is written at `kirocrew pod up <worktree>`.",
            None,
        )
    dst = unit_mod.install_unit(cfg)
    cp = systemctl("daemon-reload")
    if cp.returncode != 0:
        return f"installed pod template unit → {dst}", cp
    return f"installed pod template unit → {dst}\nsystemctl --user daemon-reload OK", cp


def health(port: int, timeout: int = 3) -> int:
    """HTTP status of the pod's /api/health, or 0 if unreachable.

    200 = open; 401/403 = serving but gated — all three mean "up".
    """
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # Loopback-only probe to the pod's own gateway on 127.0.0.1; the URL is
        # internally derived (never attacker-supplied), so the dynamic-URL SSRF
        # audit rule is a false positive here.
        with loopback_urlopen(url, timeout=timeout) as resp:  # nosemgrep
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


# --------------------------------------------------------------------------- #
# Token mint — reads the pod's OWN .local_secret (in its isolated HOME), then
# calls /api/token/local with X-Local-Secret. Keeps the secret read inside this
# process (never an agent-issued `cat`).
# --------------------------------------------------------------------------- #
def mint_token(cfg: PodConfig, name: str, ttl: str = "2h") -> str:
    secret_file = cfg.home_dir(name) / ".local_secret"
    try:
        secret = secret_file.read_text().strip()
    except FileNotFoundError as exc:
        raise PodError(
            f"no .local_secret for pod {name!r} — is it running? ({secret_file})"
        ) from exc
    port = derive_port(cfg, name)
    url = f"http://127.0.0.1:{port}/api/token/local?ttl={urllib.parse.quote(str(ttl))}"
    req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
    try:
        # Loopback-only call to the pod's own gateway on 127.0.0.1; the URL is
        # internally derived, so the dynamic-URL SSRF audit rule is a false positive.
        with loopback_urlopen(req, timeout=5) as resp:  # nosemgrep
            token = json.loads(resp.read()).get("token", "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PodError(f"token mint failed on :{port} ({name}): {exc}") from exc
    if not token:
        raise PodError(f"gateway returned empty token on :{port} ({name})")
    return token


# --------------------------------------------------------------------------- #
# Seed sanitization — deny-by-default. A seeded pod must NEVER be able to grab the
# real Slack identity, so we only ever return a config with tunnel DISABLED.
# Anything that prevents us from positively guaranteeing that (missing file, bad
# JSON) returns None → the caller skips the seed and the pod boots blank.
# --------------------------------------------------------------------------- #
def sanitized_seed_config(seed_dir: Path) -> dict | None:
    """Read ``<seed_dir>/config.json`` and return it with ``tunnel.enabled``
    forced to False, or None if it can't be safely sanitized (no file / bad JSON
    / sensitive path). Never copies any other state (DB / sessions / crons)."""
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
        data = json.loads(src_cfg.read_text())
    except (OSError, ValueError):
        print("WARN: could not parse seed config.json — skipping seed (pod boots blank)")
        return None
    if not isinstance(data, dict):
        return None
    # Deny-by-default: force OFF every messaging channel that can self-activate
    # from config.json — not just the tunnel. A seed cloned from a real config
    # (the intended ``--seed ~/.kiro/crew`` workflow) would otherwise boot live
    # Telegram / WeCom bots answering real users. Overwrite any non-dict section
    # value too, so the enabled=False guarantee can't be skipped by a falsy value.
    # (Slack has no config-level enable — it is credential-gated, and those creds
    # are scrubbed from the pod env by build_pod_env.)
    for section in ("tunnel", "telegram", "wecom"):
        if not isinstance(data.get(section), dict):
            data[section] = {}
        data[section]["enabled"] = False
    return data


def build_pod_env(cfg: PodConfig, home_dir: Path, port: int, checkout: Path) -> dict[str, str]:
    """Construct the isolated gateway environment for a pod.

    Scrubs messaging-identity creds so the pod can't inherit and re-use the live
    plane's Slack / WeCom / Telegram identity via the systemd --user manager env:
    ``SLACK_*``, ``WECOM_*`` (WECOM_BOT_ID / WECOM_SECRET), and non-AWS ``*_TOKEN``
    (covers ``TELEGRAM_BOT_TOKEN``). ``AWS_*`` is kept on purpose (pods run agent
    turns), and the ``_TOKEN`` scrub deliberately EXCLUDES ``AWS_`` so
    ``AWS_SESSION_TOKEN`` (temp creds) survives intact — scrubbing it would leave
    half a credential and break every AWS call. Config-level channel enables are
    additionally forced off by ``sanitized_seed_config`` (defense-in-depth).
    """
    env = {
        **os.environ,
        "HOME": os.environ.get("HOME", str(Path.home())),
        "KIROCREW_HOME": str(home_dir),
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
        # Inside the pod HOME so the zero-residue ``ExecStopPost`` teardown
        # reclaims it.
        "KIRO_HOME": str(home_dir / "kiro"),
        # NOTE: deliberately NO ``KIRO_HOME`` here, though it is tempting — it
        # would give the pod its own agent specs and stop pod boots rewriting the
        # machine-wide ``~/.kiro/agents``. ``KIRO_HOME`` is a DIRECTORY-WIDE
        # kiro-cli override (agents, prompts, skills, steering, settings AND
        # sessions), while KiroCrew still resolves the host paths for roughly two
        # dozen of those readers — ``session_map.py``, ``subagent_persistence.py``,
        # ``acp/{client,session_handle,session_provider}.py``,
        # ``providers/acp.py``, ``dashboard/handlers/usage.py`` and the
        # ``settings/mcp.json`` sites. Exporting it here would move where kiro-cli
        # WRITES session transcripts without moving where KiroCrew READS them, so a
        # pod restart would lose session resume and ``SessionMap`` would prune
        # mappings whose transcripts it can no longer see: a worse split brain than
        # the one this change set fixes. The write guard in ``agent.py`` covers the
        # shared-spec hazard for pods in the meantime. Setting it here is safe only
        # once those readers resolve through ``kiro_home()`` too.
        # Give the pod its OWN workspace root. Without this, `workspace_root()`
        # finds no `KIROCREW_WORKSPACE` and no `config_dir()/workspace_dir` file in
        # a fresh pod home, so it falls through to the platform default under the
        # REAL `HOME` — and every agent turn in the pod (a `chat`/`run`/`tui`
        # through `pod exec`, and the pod gateway's own sessions) would read and
        # WRITE the live workspace. `KIROCREW_WORKSPACE` is the documented override
        # (config/loader.py:220, used as-is) and `eval/runner.py` already scopes a
        # run the same way, so this is the existing mechanism rather than new
        # resolution behaviour. Placing it inside the pod HOME means the
        # zero-residue `ExecStopPost` teardown removes it with everything else.
        "KIROCREW_WORKSPACE": str(home_dir / "workspace"),
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
        or (k.endswith("_TOKEN") and not k.startswith("AWS_"))
    ]:
        env.pop(key, None)
    return env


def write_pod_config(home_dir: Path, seed: str) -> None:
    """Ensure the pod HOME exists (owner-only) with a tunnel-disabled config.json.

    Every pod — blank or seeded — gets a config with ``tunnel.enabled=False`` so
    "never grabs the live Slack identity" is guaranteed by config, not merely by
    the absence of ``SLACK_*`` in the inherited env. The HOME dir is ``0o700`` and
    ``config.json`` is ``0o600`` — the seeded config can carry provider tokens /
    API keys, which must not be world-readable on a shared host.
    """
    home_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home_dir, stat.S_IRWXU)  # 0o700 owner-only (mkdir mode is umask-masked)
    # The pod's own workspace root (see build_pod_env's KIROCREW_WORKSPACE).
    # Created here so the gateway never falls back to the live workspace.
    (home_dir / "workspace").mkdir(mode=0o700, exist_ok=True)
    dst_cfg = home_dir / "config.json"
    if dst_cfg.exists():
        return
    sanitized = sanitized_seed_config(Path(seed)) if seed else None
    cfg_data = sanitized if sanitized is not None else {"tunnel": {"enabled": False}}
    dst_cfg.write_text(json.dumps(cfg_data, indent=2))
    os.chmod(dst_cfg, stat.S_IRUSR | stat.S_IWUSR)  # 0o600 owner read/write only


def cleanup_home(cfg: PodConfig, name: str) -> int:
    """Zero-residue teardown of pod *name*'s HOME — the ``ExecStopPost`` body.

    Routed through Python (not a raw ``rm -rf {pod_root}/%i``) because the rm safety
    must NOT rely on systemd ``%i`` semantics: ``%i`` cannot contain ``/`` but CAN
    be ``..``, and the template unit is a standalone artifact that bypasses the
    CLI's ``validate_name``. Re-validate the name and confirm the target is a direct
    child of pod_root before deleting, so teardown can never escape to ``$HOME`` or
    a parent.
    """
    try:
        validate_name(name)
    except PodError:
        print(f"refusing pod cleanup for invalid instance name {name!r}")
        return 2
    root = cfg.pod_root.resolve()
    target = (cfg.pod_root / name).resolve()
    if target == root or target.parent != root:
        print(f"refusing pod cleanup: {target} is not a pod dir under {root}")
        return 2
    shutil.rmtree(target, ignore_errors=True)
    return 0


def pod_context(cfg: PodConfig, name: str) -> tuple[Path, dict[str, str]]:
    """Resolve pod *name* to ``(its own kirocrew binary, its isolated env)``.

    The single seam every pod-scoped command goes through, so ``boot``,
    :func:`exec_in_pod` and ``pod env`` cannot drift apart. Notably the env comes
    from :func:`build_pod_env`, which means a command run against a pod inherits
    the SAME messaging-credential scrubbing as the pod's own gateway — a
    hand-rolled env here would silently let a throwaway instance act as the live
    Slack / WeCom / Telegram identity.

    Raises :class:`PodError` when the pod has no pinned checkout (never brought
    up from inside a checkout) or that checkout has no provisioned venv.
    """
    validate_name(name)
    checkout_str = read_env_file(cfg, name).get("CHECKOUT")
    if not checkout_str:
        raise PodError(
            f"pod {name!r} has no pinned checkout — run `kirocrew pod up {name}` "
            f"from inside a kirocrew checkout first"
        )
    checkout = Path(checkout_str).expanduser()
    bin_path = prov.venv_bin(checkout)
    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        raise PodError(f"no kirocrew venv at {bin_path} (provision {name} first)")
    env = build_pod_env(cfg, cfg.home_dir(name), derive_port(cfg, name), checkout)
    return bin_path, env


# `pod exec` forwards to a real kirocrew, so it inherits the WHOLE CLI — including
# verbs that manage the HOST rather than any one instance. This is an ALLOWLIST
# rather than a denylist because the set of host-scoped verbs is open-ended (a
# by-name denylist repeatedly missed verbs — `stop`, then `restart`, then
# `service`): every verb below acts only on `KIROCREW_HOME` state, which
# `pod exec` has already pointed at the pod.
# Anything else — present or newly added — is refused until it is deliberately
# listed, so the failure mode of drift is "temporarily unavailable" rather than
# "silently operated on the user's live machine".
#
# Deliberately EXCLUDED, with the reason each is host-scoped, not pod-scoped:
#   setup, update  — rewrite the install and the ~/.local/bin launcher
#   app            — `apps/bridges.py` edits `~/.kiro/settings/mcp.json`, the
#                    HOST registry, which is NOT covered by KIROCREW_HOME or
#                    KIRO_HOME. (The app agent JSONs it symlinks now follow
#                    `kiro_agents_dir()`, so those land under the pod's own
#                    KIRO_HOME — but the settings registry still does not, so a
#                    pod install/uninstall would still mutate host state.)
#   stop, restart  — service-aware: `cli_server._stop` short-circuits to
#                    systemctl when no explicit --port is passed, so they hit the
#                    LIVE gateway; and `restart` additionally makes systemd run
#                    the pod unit's ExecStopPost (erasing the pod HOME) while
#                    leaving a DETACHED replacement `pod down` cannot stop
#   service        — installs/removes the machine-wide systemd unit
#   gateway        — would race a second gateway against the pod's own unit
#   pod            — pod management from inside a pod (a nested `pod down` would
#                    tear down its own supervisor)
#   cloud          — provisions resources in the user's AWS account
#   browse         — writes browser auth state outside KIROCREW_HOME
#   manifest       — emits a Slack app manifest tied to the real identity
#   run            — `task_reporter.save_progress` writes `TASK_PROGRESS.md`
#                    "next to the spec file" (`Path(run.spec_path).parent`), so
#                    `run /host/TASK.md` writes `/host/TASK_PROGRESS.md`. An
#                    IMPLICIT write outside the pod, derived from a path the user
#                    supplied as an input rather than a destination.
#   snapshot       — its destination is CONFIGURABLE (`snapshot_dir`, or a
#                    positional dir) and `--keep N` DELETES older archives beyond
#                    N. `sanitized_seed_config` only forces tunnel/telegram/wecom
#                    off, so a pod seeded from the live config inherits the user's
#                    real backup directory — `snapshot --keep 1` from a pod would
#                    prune live backups. Destructive and cross-plane.
#   doctor         — NOT read-only: `cli_doctor.py:126` does
#                    `atomic_write(agent_path, ...)` where `agent_path` is
#                    `KIRO_AGENTS_DIR / AGENT_FILENAME` (line 405), i.e. under the
#                    real HOME. It auto-adds missing MCP servers, so a pod
#                    `doctor` rewrites the LIVE agent configuration.
#   tui, chat      — `cli_chat._tui` resolves its port as
#                    `getattr(args, "port", None) or cfg…get("port", 5476)` — the
#                    CONFIG dashboard port, falling back to literal 5476, never
#                    `KIROCREW_PORT`. A pod's config names no dashboard port, so it
#                    lands on the LIVE gateway. `chat` is excluded too because
#                    `chat --tui` branches straight into `_tui` (cli.py:1818), so
#                    excluding only `tui` left the same hole open. Every OTHER
#                    client verb (`status`, `logout`, and the credential verb) goes
#                    through `cli_server.resolve_client_port`, which DOES honour
#                    `KIROCREW_PORT` — so this hazard is confined to `_tui`.
#   logs           — `cli_server._logs_cmd` runs `journalctl -u <SERVICE_NAME>`,
#                    the HOST service unit, so inside a pod it would show the LIVE
#                    gateway's journal while appearing to show the pod's. Wrong
#                    answer rather than damage, but a confidently wrong one.
#                    `kirocrew pod logs NAME` reads the pod's own unit.
#   mcp-*          — stdio server entrypoints, not user-facing commands
#
# `agent` and `workspace` ARE listed: both dispatchers were checked and operate
# only on `config_dir()` state (the config.json agents/workspaces maps), never on
# `~/.kiro/agents`. `workspace` is safe specifically because it asserts every
# source and destination `is_relative_to(config_dir())`; without that guard it
# would belong with `snapshot`. `restore` is listed because although its SOURCE
# archive path is arbitrary, that is a read — everything it writes lands in
# `config_dir()`.
#
# The inclusion test, stated once so it does not have to be rediscovered. A verb
# qualifies only if BOTH hold:
#   (a) every path it writes IMPLICITLY — anywhere the user did not name as a
#       destination — is inside `config_dir()`; and
#   (b) it deletes nothing outside `config_dir()`.
# An explicitly-named output destination is fine (`memory export -o FILE` writes
# where the user pointed it, no differently from shell redirection). What fails is
# an implicit write derived from an INPUT path (`run` → `TASK_PROGRESS.md` beside
# the spec), a host registry (`app`), a host service (`service`, `stop`,
# `restart`), a deletion driven by config (`snapshot --keep`), or a host-scoped
# read that merely LOOKS pod-scoped (`logs`).
_POD_SAFE_VERBS = frozenset(
    {
        "agent",
        "artifact",
        "config",
        "consolidate",
        "cron",
        "eval",
        "knowledge",
        "learn",
        "logout",
        "memory",
        "policy",
        "restore",
        "security",
        "spawn",
        "status",
        "token",
        "workspace",
    }
)

# The pod-native equivalent to suggest for the verbs users are most likely to try.
_POD_EQUIVALENT: dict[str, str] = {
    "stop": "kirocrew pod down {name}",
    "restart": "kirocrew pod down {name} && kirocrew pod up {name}",
    "gateway": "kirocrew pod up {name}",
    "logs": "kirocrew pod logs {name}",
}


def require_pod_safe_verb(argv: list[str], name: str) -> None:
    """Raise :class:`PodError` unless ``argv[0]`` is a pod-scoped verb.

    The verb must come FIRST. That is a deliberate constraint rather than a
    limitation to work around: allowing global flags ahead of it reintroduces the
    parsing ambiguity that made the previous denylist bypassable (``-v stop``, and
    worse ``--log-level DEBUG stop``, where the flag's value is indistinguishable
    from a verb). Flags AFTER the verb are untouched, so `-- status --json` works.
    """
    verb = argv[0] if argv else ""
    if verb in _POD_SAFE_VERBS:
        return
    hint = ""
    if verb in _POD_EQUIVALENT:
        hint = f" Use `{_POD_EQUIVALENT[verb].format(name=name)}` instead."
    elif verb.startswith("-"):
        hint = " The verb must come first; put global flags after it."
    else:
        hint = (
            " Only verbs that act on the pod's own data are allowed: "
            + ", ".join(sorted(_POD_SAFE_VERBS))
            + "."
        )
    raise PodError(f"refusing `{verb or '(nothing)'}` inside `pod exec`:{hint}")


def exec_in_pod(cfg: PodConfig, name: str, argv: list[str]) -> int:
    """``exec`` *argv* as the pod's own kirocrew, in the pod's isolated env.

    Replaces the current process on success, so the child's exit status and its
    stdio (including a TTY, which matters for ``chat`` / ``tui``) reach the caller
    untouched. Returns an exit code only when the exec itself fails.
    """
    require_pod_safe_verb(argv, name)
    bin_path, env = pod_context(cfg, name)
    # Run INSIDE the pod's workspace, not the caller's cwd. Agent verbs resolve
    # relative paths against the working directory, so inheriting the invoking
    # shell's cwd (often the live checkout) would let `-- run ./TASK.md` or a file
    # edit land outside the pod even with KIROCREW_WORKSPACE set correctly.
    workspace = Path(env["KIROCREW_WORKSPACE"])
    try:
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chdir(workspace)
    except OSError as exc:
        print(f"FATAL: could not enter the pod workspace {workspace}: {exc}")
        return 70
    try:
        os.execve(str(bin_path), [str(bin_path), *argv], env)
    except OSError as exc:  # pragma: no cover - exec failure is environmental
        print(f"FATAL: could not exec {bin_path}: {exc}")
        return 70
    return 0  # unreachable on success


# --------------------------------------------------------------------------- #
# Boot — the ExecStart body. Re-entered as ``kirocrew pod _run <name>`` by the
# systemd unit. Reads the PINNED checkout (never shells git), then exec()s the
# worktree's own gateway with an isolated HOME; never returns on success.
# --------------------------------------------------------------------------- #
def boot(cfg: PodConfig, name: str) -> int:
    """Boot the isolated gateway for pod *name*. Returns an exit code on failure;
    on success it ``exec``s and does not return."""
    validate_name(name)
    env_data = read_env_file(cfg, name)
    checkout_str = env_data.get("CHECKOUT")
    if not checkout_str:
        print(
            f"FATAL: pod {name!r} has no pinned checkout — run "
            f"`kirocrew pod up {name}` from inside a kirocrew checkout first"
        )
        return 3
    checkout = Path(checkout_str).expanduser()
    home_dir = cfg.home_dir(name)
    bin_path = prov.venv_bin(checkout)

    if not (bin_path.exists() and os.access(bin_path, os.X_OK)):
        print(f"FATAL: no kirocrew venv at {bin_path} (provision {name} first)")
        return 3
    if not (checkout / "src" / "kiro_crew" / "static" / "dist").is_dir():
        print(f"FATAL: no built dist for {name} (build the worktree first)")
        return 3

    port = derive_port(cfg, name)
    if port == cfg.live_port:
        print(f"FATAL: derived port is the live plane :{cfg.live_port} — refusing")
        return 70

    seed = env_data.get("SEED", "")
    approval = env_data.get("APPROVAL", "")
    if approval and approval not in APPROVAL_MODES:
        # `pod up` constrains the flag, but this file is hand-editable, so an
        # unknown value can still reach here. Do NOT merely drop it: omitting
        # --approval does not mean "interactive". The gateway leaves
        # ``approval_mode`` unset, and slack/events.py falls through to
        # ``cfg.agent.approval_mode``, which config/loader.py defaults to
        # "auto" -- auto-approve every tool. Dropping would therefore be the
        # LEAST restrictive outcome. Pin interactive explicitly instead.
        print(
            f"kirocrew-pod: ignoring unknown APPROVAL={approval!r} "
            f"(expected one of: {', '.join(APPROVAL_MODES)}); "
            f"forcing --approval interactive"
        )
        approval = "interactive"

    crons_raw = env_data.get("CRONS", "")
    crons = crons_raw.strip().lower() in CRONS_TRUE
    if crons_raw and not crons:
        # Same reasoning as APPROVAL above: hand-editable file, so an
        # unrecognised value falls back to the safer setting (scheduler off)
        # instead of guessing, and the pod still boots.
        print(
            f"kirocrew-pod: ignoring unrecognised CRONS={crons_raw!r} "
            f"(expected one of: {', '.join(sorted(CRONS_TRUE))}); scheduler stays off"
        )

    # Write the pod's isolated, tunnel-disabled config with owner-only perms.
    # Creates the HOME (0o700) too. Never copies DB/sessions/crons.
    write_pod_config(home_dir, seed)

    print(f"kirocrew-pod: name={name} port={port} home={home_dir} checkout={checkout}")

    pod_env = build_pod_env(cfg, home_dir, port, checkout)
    argv = ["gateway"]
    if not crons:
        argv.append("--no-crons")
    if approval:
        argv += ["--approval", approval]
    os.execve(str(bin_path), [str(bin_path), *argv], pod_env)
    return 0  # unreachable on success

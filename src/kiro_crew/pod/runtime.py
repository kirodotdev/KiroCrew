"""Pod runtime mechanics: git worktree resolution, port derivation, systemd
wrappers, boot, token mint.

Everything that talks to the host (``git``, ``systemctl --user``, ``cksum``, the
pod's ``.local_secret``) lives here so :mod:`kiro_crew.pod.cli` stays a thin verb
layer. No state is held; each function reads what it needs from a
:class:`PodConfig` (and, for worktree resolution, from git / the per-pod env file).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
# Per-pod env file (pinned CHECKOUT= / PORT= / SEED=). Values are single-quoted
# on write and unquoted on read; unknown keys are preserved on merge.
# --------------------------------------------------------------------------- #
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
def _systemctl_env() -> dict[str, str]:
    return {**os.environ}


def _run(cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_systemctl_env()
    )


def systemctl(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return _run(["systemctl", "--user", *args], timeout=timeout)


def is_active(cfg: PodConfig, name: str) -> bool:
    cp = systemctl("is-active", "--quiet", pod_unit(cfg, name))
    return cp.returncode == 0


def unit_state(cfg: PodConfig, name: str) -> tuple[str, int]:
    """(ActiveState, NRestarts) for the pod's unit — ("unknown", 0) on error.

    Lets the up-path tell a CRASHED/crash-looping worktree gateway (a broken
    build, import error, bad config) apart from one that is just slow to come up —
    so we fail fast with the gateway's own error instead of polling a dead unit
    for the full timeout.
    """
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
    """Tail the pod unit's journal — used to surface a boot failure's real cause."""
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


def health(port: int, timeout: int = 3) -> int:
    """HTTP status of the pod's /api/health, or 0 if unreachable.

    200 = open; 401/403 = serving but gated — all three mean "up".
    """
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        # Loopback-only probe to the pod's own gateway on 127.0.0.1; the URL is
        # internally derived (never attacker-supplied), so the dynamic-URL SSRF
        # audit rule is a false positive here.
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosemgrep
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
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosemgrep
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
    # (the intended ``--seed ~/.kirocrew`` workflow) would otherwise boot live
    # Telegram / WeCom bots answering real users. Overwrite any non-dict section
    # value too, so the enabled=False guarantee can't be skipped by a falsy value.
    # (Slack has no config-level enable — it is credential-gated, and those creds
    # are scrubbed from the pod env by build_pod_env.)
    for section in ("tunnel", "telegram", "wechat"):
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
        "PATH": cfg.gateway_path,
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
    bin_path = checkout / ".venv" / "bin" / "kirocrew"

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

    # Write the pod's isolated, tunnel-disabled config with owner-only perms.
    # Creates the HOME (0o700) too. Never copies DB/sessions/crons.
    write_pod_config(home_dir, seed)

    print(f"kirocrew-pod: name={name} port={port} home={home_dir} checkout={checkout}")

    pod_env = build_pod_env(cfg, home_dir, port, checkout)
    os.execve(str(bin_path), [str(bin_path), "gateway", "--no-crons"], pod_env)
    return 0  # unreachable on success

"""Provision a worktree so it can be podded: venv + built SPA dist.

A pod boots the worktree's OWN ``.venv/bin/kirocrew gateway`` serving its OWN
``static/dist`` bundle. Both are prerequisites of "a worktree that can run a
gateway at all" — not pod inventions — but they're the on-ramp friction, so this
module collapses them into one command (``kirocrew pod provision`` /
``pod up --provision``).

Cost asymmetry drives the design:
  * venv  — pure pip editable install, ~1 min, idempotent → safe to auto-run.
  * dist  — the Vite/npm SPA build, minutes → only on explicit consent.

So plain ``pod up`` auto-builds the venv but never the dist (it fails loud and
points at provision); provision / ``--provision`` does the full chain.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.env import find_node_tool, node_augmented_path


def _say(msg: str) -> None:
    """Progress goes to STDERR so a ``pod up --json`` stdout stays pure JSON."""
    print(msg, file=sys.stderr, flush=True)


def _find_python(version: str = "3.12") -> str | None:
    """Locate a pythonX.Y interpreter for the venv."""
    candidates = [
        Path.home() / ".local" / "bin" / f"python{version}",
        Path(f"/usr/bin/python{version}"),
        Path(f"/usr/local/bin/python{version}"),
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return str(c)
    return shutil.which(f"python{version}")


def venv_bin_dir(checkout: Path) -> Path:
    """Directory holding the worktree venv's console scripts.

    POSIX venvs use ``.venv/bin``, Windows ``.venv\\Scripts``. This is the ONE
    place that knows the layout — :func:`venv_bin` and the pod runtime both
    derive from it, so a built worktree is judged the same way everywhere.
    """
    return checkout / ".venv" / ("Scripts" if platform_compat.IS_WINDOWS else "bin")


def venv_bin(checkout: Path) -> Path:
    """Path to the worktree venv's ``kirocrew`` entry point.

    Booting a pod is Linux-only, but :func:`has_venv` is called on EVERY
    platform to report build state in the Dev Fleet view — so a POSIX-only path
    here would report a perfectly built Windows worktree as unbuilt.
    """
    name = "kirocrew.exe" if platform_compat.IS_WINDOWS else "kirocrew"
    return venv_bin_dir(checkout) / name


def dist_dir(checkout: Path) -> Path:
    return checkout / "src" / "kiro_crew" / "static" / "dist"


def has_venv(checkout: Path) -> bool:
    binp = venv_bin(checkout)
    return binp.exists() and os.access(binp, os.X_OK)


def has_dist(checkout: Path) -> bool:
    return dist_dir(checkout).is_dir()


def _run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    """Run a provisioning step, streaming its output to STDERR (so a concurrent
    ``pod up --json`` keeps a clean stdout). Returns the exit code."""
    _say(f"  $ {' '.join(cmd)}  (cwd={cwd})")
    # Redirect the child's stdout to our stderr so its chatter never lands on our
    # stdout; its own stderr passes through to stderr too.
    cp = subprocess.run(cmd, cwd=str(cwd), stdout=sys.stderr, env=env)
    return cp.returncode


def _npm_env() -> dict[str, str]:
    """Environment for an npm step, with the node toolchain on ``PATH``.

    Resolving the ``npm`` executable is not enough: npm spawns its own
    run-scripts (``tsc``, ``vite``) whose shebang is ``#!/usr/bin/env node``, so
    ``node`` must be findable by NAME inside the child too.
    """
    env = dict(os.environ)
    env["PATH"] = node_augmented_path(env.get("PATH", ""))
    return env


def _npm_bin() -> str | None:
    """Absolute path to ``npm``, or ``None`` with an actionable message emitted.

    ``pod provision`` runs from whatever spawned it. A login shell has the user's
    version manager active, but the Dev Fleet backend pins ``PATH`` to system bin
    dirs, and a systemd/launchd gateway inherits no version manager at all --
    so a bare ``["npm", ...]`` raised ``FileNotFoundError`` as an unhandled
    traceback. Resolve explicitly and fail with a remedy instead.
    """
    npm = find_node_tool("npm")
    if npm:
        return npm
    _say(
        "FATAL: npm not found. Kiro Crew looks for a Node toolchain in "
        "<data-home>/node-bin-dir (written by ensure-node.sh), then in "
        "mise / asdf / nvm / fnm / volta install dirs, then on PATH.\n"
        "  Fix: run `bash ensure-node.sh` in the main checkout to install "
        "Node, or set KIROCREW_NODE_BIN_DIR=/abs/path/to/node/bin."
    )
    return None


def ensure_venv(checkout: Path) -> bool:
    """Create the worktree's editable venv if missing. Idempotent. Returns True if
    the venv is ready afterward."""
    if has_venv(checkout):
        return True
    py = _find_python()
    if not py:
        _say("FATAL: no python3.12 found (need it to build the venv)")
        return False
    _say(f"[provision] creating venv for {checkout.name} (one-time, ~1 min)…")
    venv_dir = checkout / ".venv"
    if _run([py, "-m", "venv", str(venv_dir)], checkout) != 0:
        return False
    pip = venv_bin_dir(checkout) / ("pip.exe" if platform_compat.IS_WINDOWS else "pip")
    # Upgrade pip first — `pip install --group` (PEP 735) needs pip >= 25.1, and a
    # fresh `python -m venv` ships an older pip on many hosts.
    _run([str(pip), "install", "--quiet", "--upgrade", "pip"], checkout)
    # Install runtime deps AND the PEP 735 `dev` dependency-group (pytest, flake8,
    # isort, mypy, …) so the documented build gate can run inside the pod venv.
    # If `--group` is unsupported (pip < 25.1) the command exits
    # nonzero, so fall back to a runtime-only editable install and warn — never
    # hard-fail provisioning just because the dev extras could not be installed.
    if _run(
        [str(pip), "install", "--editable", str(checkout), "--group", "dev"],
        checkout,
    ) != 0:
        _say(
            "[provision] `pip install --group dev` failed (pip < 25.1?) — falling "
            "back to a runtime-only editable install; dev tools (pytest/flake8) "
            "were skipped, so the build gate can't run in this venv"
        )
        if _run([str(pip), "install", "--editable", str(checkout)], checkout) != 0:
            return False
    return has_venv(checkout)


def _has_node_modules(website: Path) -> bool:
    """True when ``website/`` already has installed npm deps (with ``tsc``), so the
    install step can be skipped on the fast idempotent path. ``tsc`` is the build's
    key binary; its presence stands in for "deps are installed"."""
    return (website / "node_modules" / ".bin" / "tsc").exists()


def ensure_node_modules(website: Path) -> bool:
    """Install ``website/`` npm dependencies if missing.

    A fresh worktree has no ``website/node_modules`` (gitignored), so ``npm run
    build`` dies with ``tsc: command not found``. Install deps first: prefer
    ``npm ci`` (clean, lockfile-exact) and, if that fails (e.g. lockfile drift),
    fall back to ``npm install --no-package-lock``. The ``--no-package-lock``
    flag keeps the fallback NON-MUTATING: it installs into ``node_modules``
    without rewriting the tracked ``website/package-lock.json`` — provisioning
    must never dirty tracked files (accidental lockfile churn would block
    prune/rebase). Skips entirely when ``node_modules`` is already present, so
    re-provisioning stays fast. Returns True when deps are ready."""
    if _has_node_modules(website):
        return True
    npm = _npm_bin()
    if npm is None:
        return False
    env = _npm_env()
    _say("[provision] installing website npm deps (node_modules missing)…")
    if _run([npm, "ci"], website, env) == 0:
        return True
    _say(
        "[provision] `npm ci` failed (lockfile drift?) — falling back to "
        "`npm install --no-package-lock` (non-mutating: won't rewrite the "
        "tracked package-lock.json)"
    )
    return _run([npm, "install", "--no-package-lock"], website, env) == 0


def build_dist(checkout: Path) -> bool:
    """Build the worktree's SPA dist (the slow step): ``npm run build`` in
    ``website/`` (→ ``website/dist``), then stage it into the served
    ``src/kiro_crew/static/dist``. Returns True if the dist exists afterward."""
    if has_dist(checkout):
        return True
    website = checkout / "website"
    if not website.is_dir():
        _say(f"FATAL: no website/ directory at {website}")
        return False
    _say(
        f"[provision] building dist for {checkout.name} "
        f"(slow — Vite SPA build, several minutes)…"
    )
    # A fresh worktree has no website/node_modules (gitignored); install deps
    # before building or `npm run build` dies with `tsc: command not found`.
    if not ensure_node_modules(website):
        _say("FATAL: failed to install website npm deps")
        return False
    npm = _npm_bin()
    if npm is None:
        return False
    if _run([npm, "run", "build"], website, _npm_env()) != 0:
        _say("FATAL: npm run build failed")
        return False
    src_dist = website / "dist"
    if not src_dist.is_dir():
        _say(f"FATAL: npm build produced no dist at {src_dist}")
        return False
    # Stage website/dist → the served static/dist (replace any stale copy).
    dst = dist_dir(checkout)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copytree(src_dist, dst)
    return has_dist(checkout)


def provision(checkout: Path, build: bool = True) -> bool:
    """Full on-ramp: ensure venv (always) + build dist (when build=True).

    Returns True only when the worktree is fully pod-able afterward. When
    build=False, returns True if the venv is ready (dist left to the caller).
    """
    if not ensure_venv(checkout):
        return False
    if not build:
        return True
    if not build_dist(checkout):
        return False
    _say(f"[provision] {checkout.name} is ready — `kirocrew pod up {checkout.name}`")
    return True

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


def venv_bin(checkout: Path) -> Path:
    return checkout / ".venv" / "bin" / "kirocrew"


def dist_dir(checkout: Path) -> Path:
    return checkout / "src" / "kiro_crew" / "static" / "dist"


def has_venv(checkout: Path) -> bool:
    binp = venv_bin(checkout)
    return binp.exists() and os.access(binp, os.X_OK)


def has_dist(checkout: Path) -> bool:
    return dist_dir(checkout).is_dir()


def _run(cmd: list[str], cwd: Path) -> int:
    """Run a provisioning step, streaming its output to STDERR (so a concurrent
    ``pod up --json`` keeps a clean stdout). Returns the exit code."""
    _say(f"  $ {' '.join(cmd)}  (cwd={cwd})")
    # Redirect the child's stdout to our stderr so its chatter never lands on our
    # stdout; its own stderr passes through to stderr too.
    cp = subprocess.run(cmd, cwd=str(cwd), stdout=sys.stderr)
    return cp.returncode


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
    pip = venv_dir / "bin" / "pip"
    _run([str(pip), "install", "--quiet", "--upgrade", "pip"], checkout)
    if _run([str(pip), "install", "--editable", str(checkout)], checkout) != 0:
        return False
    return has_venv(checkout)


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
    if _run(["npm", "run", "build"], website) != 0:
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

"""Preflight: are the external tools a run depends on actually present?

A run shells out to a handful of binaries. Discovering a missing one halfway
through a cycle wastes the whole cycle, so the UI asks this up front and can show
what to install.

Reports rather than repairs. The upstream version could install its internal
toolchain packages itself; here the hard dependencies (``git`` plus the forge CLI
matching the configured target — ``gh`` for GitHub, ``glab`` for GitLab) are
things a user installs and authenticates deliberately — silently installing an
authenticated CLI on someone's behalf is not this app's business. Only the
optional linter, which is a plain pip package in the app's own environment, is
offered as an install.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any

from .clone_setup import PROVIDER_GITHUB, PROVIDER_GITLAB, provider_for_url
from .store import config_path, read_json

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 15.0


def _which(binary: str) -> str:
    return shutil.which(binary) or ""


def _forge_cli_authenticated(binary: str) -> tuple[bool, str]:
    """Whether a forge CLI (``gh``/``glab``) has a live login.

    Presence on PATH is not enough: an unauthenticated CLI fails only when a
    pull/merge request is drafted, which is the worst moment to find out.
    """
    if not _which(binary):
        return False, f"{binary} is not on PATH"
    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {binary} auth status: {exc}"
    if proc.returncode != 0:
        return False, f"{binary} is present but not logged in — run `{binary} auth login`"
    return True, "authenticated"


def _gh_authenticated() -> tuple[bool, str]:
    return _forge_cli_authenticated("gh")


def _glab_authenticated() -> tuple[bool, str]:
    return _forge_cli_authenticated("glab")


def _configured_provider() -> str:
    """The persisted target's forge, or ``""`` when no target is configured yet.

    Prefers the ``provider`` key setup persists; falls back to deriving it from
    ``target_url`` so a config written before the key existed still dispatches.
    """
    cfg = read_json(config_path(), {}) or {}
    persisted = str(cfg.get("provider") or "").strip().lower()
    if persisted in (PROVIDER_GITHUB, PROVIDER_GITLAB):
        return persisted
    return provider_for_url(str(cfg.get("target_url") or ""))


def check_deps() -> dict[str, Any]:
    """Report every dependency, whether it is satisfied, and how to fix it.

    ``required`` entries block a run; an unsatisfied optional entry only narrows
    what discovery can find. Only the forge CLI matching the configured target is
    required — with no target configured yet, neither CLI can block setup.
    """
    provider = _configured_provider()
    git_path = _which("git")
    gh_ok, gh_detail = _gh_authenticated()
    glab_ok, glab_detail = _glab_authenticated()
    ruff_path = _which("ruff")

    deps: list[dict[str, Any]] = [
        {
            "id": "git",
            "name": "git",
            "required": True,
            "ok": bool(git_path),
            "detail": git_path or "not found on PATH",
            "fix": "install git",
            "installable": False,
        },
        {
            "id": "gh",
            "name": "GitHub CLI (gh)",
            "required": provider == PROVIDER_GITHUB,
            "ok": gh_ok,
            "detail": gh_detail,
            "fix": "install the GitHub CLI, then run `gh auth login`",
            "installable": False,
        },
        {
            "id": "glab",
            "name": "GitLab CLI (glab)",
            "required": provider == PROVIDER_GITLAB,
            "ok": glab_ok,
            "detail": glab_detail,
            "fix": "install the GitLab CLI, then run `glab auth login`",
            "installable": False,
        },
        {
            "id": "ruff",
            "name": "ruff (grounds bug discovery)",
            "required": False,
            "ok": bool(ruff_path),
            "detail": ruff_path or "not found — discovery falls back to a compile check",
            "fix": "install ruff into the app environment",
            "installable": True,
        },
    ]
    blocking = [d["id"] for d in deps if d["required"] and not d["ok"]]
    return {"deps": deps, "ok": not blocking, "blocking": blocking}


def install_deps() -> dict[str, Any]:
    """Install the optional dependencies that can be installed safely.

    Only ``ruff``, and only into the interpreter already running this app — never
    a system-wide install, and never an authenticated CLI.
    """
    if _which("ruff"):
        return {"ok": True, "installed": [], "detail": "ruff already present"}
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "ruff"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "installed": [], "error": f"install failed: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {"ok": False, "installed": [], "error": f"pip failed: {tail[0][:200]}"}
    return {"ok": True, "installed": ["ruff"], "detail": "ruff installed"}

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
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.sandbox_spawn import scrub_env
from kiro_crew.security import redact_and_truncate
from kiro_crew.subprocess_utf8 import UTF8_TEXT

from ..profiles import target_provider
from ..profiles.gitlab_repo.glab_cli import gitlab_url_host, glab_env
from ..spine.push_policy import strip_credential_env
from .clone_setup import PROVIDER_GITHUB, PROVIDER_GITLAB
from .store import config_path, read_json

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT_S = 15.0


def _which(binary: str) -> str:
    """*binary* on PATH, or ``""``.

    On Windows a bare-name ``shutil.which`` searches the current directory
    first, so there only PATH's absolute entries are asked, by full path.
    """
    if os.name == "nt":
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            found = shutil.which(os.path.join(entry, binary)) if os.path.isabs(entry) else None
            if found:
                return found
        return ""
    return shutil.which(binary) or ""


def _forge_cli_authenticated(
    binary: str, *, host: str = "", env: "dict[str, str] | None" = None
) -> tuple[bool, str]:
    """Whether a forge CLI (``gh``/``glab``) has a live login.

    Presence on PATH is not enough: an unauthenticated CLI fails only when a
    pull/merge request is drafted, which is the worst moment to find out. A
    *host* asks about that host's login alone (``--hostname``).
    """
    if not _which(binary):
        return False, f"{binary} is not on PATH"
    argv = [binary, "auth", "status"] + (["--hostname", host] if host else [])
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=_PROBE_TIMEOUT_S,
            env=env,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run {binary} auth status: {exc}"
    if proc.returncode != 0:
        return False, f"{binary} is present but not logged in — run `{binary} auth login`"
    return True, "authenticated"


def gate_pytest_problem() -> str:
    """Why the gate's interpreter cannot run pytest, or ``""`` when it can.

    Every gate step runs ``sys.executable -m pytest``. A packaged app's bundled
    interpreter ships without pytest, so without this probe each candidate fails
    "reproducing test does not collect" and the harness fault is reported as the
    agent's. Run from an empty directory so no target conftest is loaded, with the
    environment the gate's own runs get: the scrubbed host environment (so a
    user-site pytest stays importable) with ``PYTEST_ADDOPTS`` cleared.
    """
    env = strip_credential_env(scrub_env())
    env["PYTEST_ADDOPTS"] = ""
    try:
        with tempfile.TemporaryDirectory() as empty:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "--version"],
                cwd=empty,
                env=env,
                capture_output=True,
                timeout=_PROBE_TIMEOUT_S,
                **UTF8_TEXT,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run {sys.executable} -m pytest: {exc}"
    if proc.returncode != 0:
        return (
            f"pytest is not installed in the interpreter the test gate uses ({sys.executable}), "
            "so no test can run"
        )
    return ""


def _gh_authenticated() -> tuple[bool, str]:
    return _forge_cli_authenticated("gh")


def _glab_authenticated(host: str) -> tuple[bool, str]:
    """``glab``'s login on the target *host* only, with the host-pinned environment.

    An unpinned ``glab auth status`` checks every host in glab's config with the
    ambient gitlab.com token, so it would send that token to a self-managed host.
    """
    if not host:
        return False, "the GitLab target has no host"
    return _forge_cli_authenticated("glab", host=host, env=glab_env(host))


def _configured_provider() -> str:
    """The configured target's forge, or ``""`` when no target is configured yet."""
    cfg = read_json(config_path(), {}) or {}
    if not str(cfg.get("target_url") or "").strip():
        return ""
    return target_provider(cfg)


def _configured_host() -> str:
    """The configured target's canonical GitLab host (port kept), or ``""``."""
    cfg = read_json(config_path(), {}) or {}
    return gitlab_url_host(str(cfg.get("target_url") or ""))


def check_deps() -> dict[str, Any]:
    """Report every dependency, whether it is satisfied, and how to fix it.

    ``required`` entries block a run; an unsatisfied optional entry only narrows
    what discovery can find. Only the forge CLI matching the configured target is
    required — with no target configured yet, neither CLI can block setup.
    """
    provider = _configured_provider()
    git_path = _which("git")
    gh_ok, gh_detail = _gh_authenticated()
    if provider == PROVIDER_GITLAB:
        glab_ok, glab_detail = _glab_authenticated(_configured_host())
    else:
        # glab's login is checked only for a GitLab target, against that host.
        glab_path = _which("glab")
        glab_ok, glab_detail = bool(glab_path), glab_path or "glab is not on PATH"
    ruff_path = _which("ruff")
    pytest_problem = gate_pytest_problem()

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
            "id": "pytest",
            "name": "pytest (runs the target's tests)",
            "required": True,
            "ok": not pytest_problem,
            "detail": pytest_problem or sys.executable,
            "fix": "run Kiro Crew from a Python environment that has pytest installed",
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
    cmd = platform_compat.isolated_python_argv("-m", "pip", "install", "--quiet", "ruff")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=300.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "installed": [], "error": f"install failed: {exc}"}
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        # pip inherits the gateway environment, so an authenticated private
        # index echoes its request URL — token and all — to stderr on an auth
        # failure. The dashboard route that serves this payload redacts what
        # it sends, but its regexes need the full credential shape to match:
        # bounding first can cut a token mid-match, leaving a fragment no
        # downstream pass can recognise. redact_and_truncate scrubs the whole
        # line BEFORE its bound, so a recognised credential straddling the
        # 200-char boundary cannot leak as an unredacted partial.
        safe_tail = redact_and_truncate(tail[0], 200)
        return {"ok": False, "installed": [], "error": f"pip failed: {safe_tail}"}
    return {"ok": True, "installed": ["ruff"], "detail": "ruff installed"}

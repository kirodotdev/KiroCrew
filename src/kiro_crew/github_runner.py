"""Shared hardened runner for the GitHub CLI (``gh``).

Single source of the gh TRUST POLICY — binary validation, resolution order,
and the child-environment key set — for every ``gh``-spawning surface: the
dashboard's PR sidebar (``dashboard/handlers/source_providers.py``), Issue
Radar (``apps/builtins/issue_radar/backend/github_client.py``), and Code
Review Sage (``apps/builtins/code_review_sage/sage_lib/discovery.py`` /
``pipeline.py``). Each previously carried its own copy of the hardened-runner
pattern, so a hardening fix had to land in three places and a missed copy
silently kept the weaker guard.

Spawning is shared for the sync app-side callers only: Issue Radar and Sage
route every spawn through :func:`run_gh` below, while the sidebar keeps its
own async, sandbox-routed, output-bounded spawn (``_run_json``) and consumes
only this module's validation/candidates/env-key policy. A spawn-level
hardening change must therefore land in ``run_gh`` AND ``_run_json`` — two
places, down from four, with the policy itself in one.

The shared pieces:

* **Trusted-binary resolution** — :func:`validate_provider_executable` and
  :func:`provider_executable_candidates` (the policy that refuses a binary
  owned by another user, a world-writable one, or one inside the
  agent-writable project/workspace tree), plus :func:`resolve_gh`, the
  caller-facing resolver with override-env and caching semantics.
* **Minimal child environment** — :func:`gh_env`, the one canonical
  passthrough list of gh-scoped auth/network/TLS variables on top of the
  platform's safe-key base. Ambient SSH-agent/git-ssh identity is stripped:
  ``gh api`` authenticates with its own token/config over HTTPS and has no
  business presenting the gateway's ssh identity.
* **The sync spawn chokepoint** — :func:`run_gh`, which enforces a validated
  absolute binary, the minimal env, a bounded timeout, and a SEL audit event
  on success, failure, and timeout for every sync app-side caller.
* **URL parsing** — :func:`parse_github_repo_url` and :class:`RepoUrlError`.

This module MUST NOT import ``kiro_crew.dashboard.*`` (at module scope or
lazily): it exists to dissolve that cross-layer dependency. Heavy runtime deps
(``kiro_crew.sel``, ``kiro_crew.apps.registry``, ``kiro_crew.config.loader``)
are imported lazily inside functions so Code Review Sage's standalone import
path (``sage_lib`` without the Kiro Crew runtime) keeps working for callers
that guard their import of this module.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class SetupError(RuntimeError):
    """``gh`` is missing or unusable on this host — a setup problem the user
    must fix (install / sign in / fix an override path), distinct from a
    transient API failure. Callers wrap this into their own error taxonomy
    (``SourceProviderError``, Issue Radar's ``GhSetupError``, Sage's
    ``GhSetupError``) so route-level handlers and UI treatment are untouched.
    """


class RepoUrlError(ValueError):
    """Raised when a repo URL is not a well-formed, supported provider URL.

    Callers map this to HTTP 400 (bad client input), as distinct from an
    upstream provider failure (502).
    """


# Opt-in hardening for shared/multi-tenant hosts: restore the historical rule
# that a provider CLI must be root-owned and unwritable by the gateway user.
# Off by default — see validate_provider_executable for the current policy.
STRICT_PROVIDER_BIN_ENV = "KIROCREW_PROVIDER_BIN_STRICT"
TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
# Well-known install dirs searched (in order) before the ambient PATH. Shared
# by every gh/glab-spawning surface so all panels accept exactly the same set
# of CLI locations and never drift.
PROVIDER_EXECUTABLE_DIRS = (
    "/usr/local/libexec/kirocrew",
    "/usr/libexec/kirocrew",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/home/linuxbrew/.linuxbrew/bin",
)
PROVIDER_EXECUTABLE_CANDIDATES = {
    executable: tuple(
        f"{directory}/{executable}" for directory in PROVIDER_EXECUTABLE_DIRS
    )
    for executable in ("gh", "glab")
}

# Generic operator override for the gh binary, honored by every caller after
# its own caller-specific override (KIROCREW_ISSUE_RADAR_GH / KIROCREW_SAGE_GH).
GH_BIN_ENV = "KIROCREW_GH_BIN"

# gh's own auth + network/TLS vars, forwarded (when present) on top of the
# platform's minimal safe-key base; everything else in the parent env is
# dropped. This is the canonical union for every gh spawn path — each key is
# gh-scoped auth/network/TLS config, so the union adds no new secret class to
# the child.
GH_ENV_PASSTHROUGH = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "GH_HOST", "GH_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)

# Ambient identity a gh child must never inherit: `gh api` authenticates with
# its own token/config over HTTPS, so the gateway's ssh agent socket and git
# ssh overrides are pure surplus credential surface. Compared upper-cased for
# the same reason registry.anonymous_git_env folds: on Windows os.environ
# yields upper-cased keys, and a missed match here would PASS a credential.
_AMBIENT_CREDENTIAL_ENV_KEYS = frozenset(
    {"SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND"}
)


def path_parents(path: Path) -> list[Path]:
    """Return every parent through the filesystem root."""
    parents: list[Path] = []
    current = path.parent
    while True:
        parents.append(current)
        if current.parent == current:
            return parents
        current = current.parent


def strict_provider_bins() -> bool:
    """True when the operator opted into the root-owned-only provider policy."""
    return os.environ.get(STRICT_PROVIDER_BIN_ENV, "").strip().lower() in TRUTHY_ENV_VALUES


def agent_writable_roots() -> tuple[Path, ...]:
    """Trees the agent itself writes: the active project checkout and the LLM
    workspace root (worktrees, venvs, scratch files, downloaded repos).

    A provider CLI resolved inside one of these is refused — a repo-planted
    ``gh`` shim is the substitution vector the model itself controls, and it is
    the same check codex applies to its own sandbox helper (reject a binary
    found inside the workspace, accept anything else).
    """
    raw_roots = [os.environ.get("KIROCREW_PROJECT_DIR")]
    try:
        from kiro_crew.config.loader import workspace_root

        raw_roots.append(str(workspace_root()))
    except Exception:  # pragma: no cover - config unavailable in isolation
        logger.debug("workspace root unavailable for provider CLI validation", exc_info=True)
    roots: list[Path] = []
    for raw in raw_roots:
        if not raw:
            continue
        try:
            roots.append(Path(raw).resolve())
        except OSError:
            continue
    return tuple(roots)


def check_provider_path_component(path: Path, *, label: str, uid: int, strict: bool) -> None:
    """Apply the ownership/permission policy to one path component."""
    try:
        path_stat = path.stat()
    except OSError as exc:
        raise ValueError("executable hierarchy is not accessible") from exc
    if strict:
        if path_stat.st_uid != 0:
            raise ValueError(f"{label} is not root-owned")
        if path_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or os.access(path, os.W_OK):
            raise ValueError(f"{label} is writable by the gateway user")
        return
    # Relaxed policy: the gateway user's own installs are fine; a binary owned
    # by a third account or writable by the whole host is not.
    if path_stat.st_uid not in (0, uid):
        raise ValueError(f"{label} is owned by another user (uid {path_stat.st_uid})")
    if path_stat.st_mode & stat.S_IWOTH:
        # A world-writable DIRECTORY is tolerated when it is sticky (`/tmp`,
        # 1777): only the owner may replace an entry, so the uid check above
        # still decides. Without the sticky bit any local account can swap the
        # entry out, and a world-writable FILE can be rewritten in place.
        if not (stat.S_ISDIR(path_stat.st_mode) and path_stat.st_mode & stat.S_ISVTX):
            raise ValueError(f"{label} is world-writable")


def validate_provider_executable(candidate: str) -> str:
    """Return the canonical path of a provider CLI we will run, or raise.

    Default policy — *if `gh` works in your terminal, it works here*. Any
    executable the gateway user could run interactively is accepted, including
    the ordinary user-owned Homebrew/Linuxbrew/asdf installs: requiring a
    root-owned copy made every stock ``brew install gh`` fail and pushed users
    into a ``sudo cp`` ritual for a CLI they had already installed and
    authenticated. What stays refused is provenance the user did not choose:

    * a binary (or parent dir) owned by another unprivileged account,
    * anything world-writable, e.g. a ``/tmp`` shim (a world-writable *directory*
      is tolerated only when sticky, where the owner check still decides),
    * anything inside the agent's project checkout or workspace root, the one
      substitution vector the model itself controls (``agent_writable_roots``).

    Provider children still receive only a minimal, provider-scoped env (no
    AWS/Slack/gateway secrets), and every spawn is SEL-audited — containment
    and audit carry the trust boundary instead of binary provenance.

    A gateway running as **root** is refused outright, in both modes: every
    process it spawns (including the agent's own shell) would be root too, which
    makes the ownership and agent-tree checks vacuous.

    Set ``KIROCREW_PROVIDER_BIN_STRICT=1`` on shared or multi-tenant hosts to
    restore the previous rule: canonical, symlink-free, root-owned and
    unwritable by the gateway user through every parent.
    """
    if not os.path.isabs(candidate):
        raise ValueError("path must be absolute")
    getuid = getattr(os, "getuid", None)
    geteuid = getattr(os, "geteuid", getuid)
    if getuid is None or geteuid is None:
        raise ValueError("filesystem ownership checks are unavailable")
    if geteuid() == 0:
        raise ValueError("provider execution is disabled for a root gateway")
    strict = strict_provider_bins()

    original = Path(candidate)
    try:
        resolved = original.resolve(strict=True)
    except OSError as exc:
        raise ValueError("path does not exist") from exc
    if strict and original != resolved:
        raise ValueError("path must be canonical and contain no symlinks")

    try:
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("path is not a regular file")
    except OSError as exc:
        raise ValueError("executable hierarchy is not accessible") from exc
    if not os.access(resolved, os.X_OK):
        raise ValueError("file is not executable")

    if not strict:
        for root in agent_writable_roots():
            if resolved == root or root in resolved.parents:
                raise ValueError(f"executable is inside the agent-writable tree {root}")

    uid = geteuid()
    check_provider_path_component(resolved, label="executable", uid=uid, strict=strict)
    # A symlink's own directory chain is part of the provenance too (relaxed
    # mode allows symlinks, so /opt/homebrew/bin gets checked as well).
    parents = list(path_parents(resolved))
    if not strict and original != resolved:
        parents += [p for p in path_parents(original) if p not in parents]
    for parent in parents:
        try:
            if not stat.S_ISDIR(parent.stat().st_mode):
                raise ValueError("executable parent is not a directory")
        except OSError as exc:
            raise ValueError("executable hierarchy is not accessible") from exc
        check_provider_path_component(parent, label="executable parent", uid=uid, strict=strict)
    return str(resolved)


def provider_executable_candidates(executable: str) -> tuple[str, ...]:
    """Absolute paths to try for *executable*, in resolution order.

    The well-known install dirs come first (a managed root-owned copy still
    wins when one exists), then every ``PATH`` hit — so the install the user
    already runs from their terminal is found even when it lives somewhere this
    module has never heard of (asdf, mise, ``~/.local/bin``). ``PATH`` is not
    consulted in strict mode, which by definition only trusts system dirs.
    """
    ordered: dict[str, None] = dict.fromkeys(PROVIDER_EXECUTABLE_CANDIDATES.get(executable, ()))
    if not strict_provider_bins():
        for entry in (os.environ.get("PATH") or "").split(os.pathsep):
            if not entry:
                continue
            found = os.path.join(entry, executable)
            if os.path.isfile(found) and os.access(found, os.X_OK):
                ordered.setdefault(os.path.abspath(found), None)
    return tuple(ordered)


# Resolution results keyed by (override env NAME, its value, the generic
# KIROCREW_GH_BIN value) so a changed override never serves a stale binary
# while repeat calls skip the stat-heavy validation walk.
_RESOLVE_CACHE: dict[tuple[str, str | None, str | None], str] = {}


def reset_cache() -> None:
    """Forget every cached gh resolution (test hook and operator-facing reset)."""
    _RESOLVE_CACHE.clear()


def resolve_gh(*, override_env: str = "", cache: bool = True) -> str:
    """Absolute path to an acceptable ``gh``, or raise :class:`SetupError`.

    Resolution order: the caller-specific *override_env* variable, then the
    generic ``KIROCREW_GH_BIN``, then :func:`provider_executable_candidates`
    (well-known install dirs, then the ambient ``PATH``). An override variable
    that is SET — even to the empty string — is validated and fails loudly: a
    set-but-wrong override is an operator mistake to surface, not to silently
    skip (silently ignoring it would fall through to a binary the operator was
    explicitly trying to avoid).

    Windows is refused outright: every consumer of this runner is POSIX-only
    (the validation policy is built on POSIX ownership semantics).
    """
    if sys.platform == "win32":
        raise SetupError(
            "gh execution requires a POSIX platform (macOS/Linux); "
            "run the Kiro Crew gateway under WSL on Windows"
        )
    key = (
        override_env,
        os.environ.get(override_env) if override_env else None,
        os.environ.get(GH_BIN_ENV),
    )
    if cache and key in _RESOLVE_CACHE:
        return _RESOLVE_CACHE[key]

    override_names = ([override_env] if override_env else []) + [GH_BIN_ENV]
    for name in override_names:
        value = os.environ.get(name)
        if value is None:
            continue
        try:
            resolved = validate_provider_executable(value)
        except ValueError as exc:
            raise SetupError(f"{name}={value!r} failed validation: {exc}") from exc
        if cache:
            _RESOLVE_CACHE[key] = resolved
        return resolved

    last_error = ""
    for candidate in provider_executable_candidates("gh"):
        try:
            resolved = validate_provider_executable(candidate)
        except ValueError as exc:
            message = str(exc)
            # "does not exist" is noise on a host that simply lacks that dir;
            # keep the most informative rejection for the setup message.
            if message != "path does not exist":
                last_error = message
            continue
        if cache:
            _RESOLVE_CACHE[key] = resolved
        return resolved

    hint = override_env or GH_BIN_ENV
    detail = f" (last check: {last_error})" if last_error else ""
    raise SetupError(
        f"no usable `gh` CLI found on this host{detail} — install it "
        "(`brew install gh` or your distro's package manager) and run "
        f"`gh auth login`, or set {hint} to an absolute gh path"
    )


def gh_env(pin_host: str = "") -> dict[str, str]:
    """A minimal environment for ``gh``: the platform's safe-key base
    (PATH/HOME/XDG/…) plus gh's own auth + network/TLS vars when set — never
    the gateway's full environment, so unrelated secrets (AWS/Slack/…) can
    never reach the child. Ambient ssh-agent/git-ssh identity is stripped too:
    ``gh`` authenticates with its own token/config over HTTPS.

    ``pin_host`` sets ``GH_HOST`` so bare API paths that do not pass
    ``--hostname`` cannot drift to a configured enterprise default. Callers
    that always pin per-call via ``--hostname`` leave it empty.
    """
    # Lazy: keeps this module importable without pulling the app registry in,
    # and lets Sage's standalone path guard the import of this module alone.
    from kiro_crew.apps.registry import minimal_env

    env = minimal_env(**{k: os.environ[k] for k in GH_ENV_PASSTHROUGH if k in os.environ})
    for env_key in [k for k in env if k.upper() in _AMBIENT_CREDENTIAL_ENV_KEYS]:
        del env[env_key]
    env["GH_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    if pin_host:
        env["GH_HOST"] = pin_host
    return env


def _audit_run(
    caller: str, target: str, outcome: str, *, error: str = "", critical: bool = False
) -> None:
    """SEL event for a gh spawn (reads and writes).

    ``critical=True`` is the audit-or-deny half of the contract: the failure
    propagates so the caller can refuse the spawn (used for the pre-spawn
    ``invoked`` event — a gh call must never run unaudited). Outcome events
    are best-effort — the spawn already happened and its ``invoked`` record
    landed, so a failed outcome write is logged at warning, never silently.

    The operation name is namespaced from *caller* (``core:issue-radar`` →
    ``issue_radar.gh_run``) so each surface keeps its historical SEL operation
    identity while the emission point is shared.
    """
    namespace = caller.split(":", 1)[-1].replace("-", "_") or "github_runner"
    try:
        from kiro_crew.sel import sel  # lazy: heavy runtime dep, see module docstring

        sel().log_api_access(
            caller=caller,
            operation=f"{namespace}.gh_run",
            outcome=outcome,
            source="builtin-app",
            resources=target[:200],
            error=error[:200] if error else "",
            critical=critical,
        )
    except Exception:
        if critical:
            raise
        # Best-effort, but never silent: an unaudited outcome is an audit-trail
        # gap an operator should be able to notice in the logs.
        logger.warning("SEL gh spawn audit failed for %s", caller, exc_info=True)


def run_gh(
    argv: Sequence[str],
    *,
    timeout: float,
    input_text: str | None = None,
    audit_caller: str,
    pin_host: str = "",
) -> subprocess.CompletedProcess[str]:
    """Single spawn chokepoint for the sync app-side ``gh`` callers.

    ``argv[0]`` must be the validated absolute path produced by
    :func:`resolve_gh` (or a caller wrapper over it) — a relative or bare name
    is refused so no call site can quietly regress to a PATH lookup. The child
    receives exactly :func:`gh_env` (minimal, gh-scoped — this is what keeps
    unrelated gateway secrets away from a substituted or compromised gh), a
    bounded *timeout*, and SEL audit events tagged with *audit_caller*
    (``core:issue-radar``, ``core:code-review-sage``, …): a fail-closed
    ``invoked`` event before the spawn (audit unavailable ⇒ the call is
    refused with :class:`SetupError`), then a best-effort outcome event on
    success, non-zero exit, timeout, and spawn ``OSError``. ``pin_host`` is forwarded to :func:`gh_env`
    for callers whose bare API paths never pass ``--hostname`` and must not
    drift to an ambient ``GH_HOST``.

    Error mapping is deliberately transparent: ``subprocess.TimeoutExpired``
    and ``FileNotFoundError`` are re-raised (after auditing) so each caller
    keeps its own error taxonomy, and a non-zero exit is returned as-is for
    the caller to classify.

    NOT sandbox-routed today: these sync callers historically spawned bare and
    this refactor is behavior-preserving. Strict-mode sandboxing would hide
    ``~/.config/gh`` + the keychain and break auth, though the sidebar's async
    path shows standard-mode routing is compatible — adopting it here is a
    follow-up, not a constraint. The trusted-binary requirement, minimal env,
    and SEL audit above are the defense-in-depth in the meantime.
    """
    if not argv or not os.path.isabs(argv[0]):
        raise SetupError(
            "run_gh requires a validated absolute gh path as argv[0] — resolve it "
            "with resolve_gh()"
        )
    operation = f"gh {' '.join(argv[1:3])}"  # e.g. "gh api repos/…" (bounded)
    # Audit-or-deny, matching the sidebar's _run_json: the invoked event is
    # written synchronously and critically BEFORE the spawn, so unwritable or
    # full SEL storage refuses the gh call instead of running it unaudited.
    try:
        _audit_run(audit_caller, operation, "invoked", critical=True)
    except Exception as exc:
        raise SetupError(
            "gh spawn audit unavailable — refusing to run gh unaudited"
        ) from exc
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True, text=True, timeout=timeout, check=False,
            input=input_text, env=gh_env(pin_host=pin_host),
        )
    except FileNotFoundError:
        _audit_run(audit_caller, operation, "failure", error="gh not found")
        raise
    except subprocess.TimeoutExpired:
        _audit_run(audit_caller, operation, "failure", error=f"timeout after {timeout}s")
        raise
    except OSError as exc:
        # A cached binary can go bad between resolution and spawn (deleted is
        # FileNotFoundError above; chmod'd or replaced with a non-executable
        # lands here as PermissionError / "Exec format error"). Audit with the
        # coarse exception class only — no path or errno text — then let the
        # caller's own error taxonomy handle it.
        _audit_run(audit_caller, operation, "failure", error=type(exc).__name__)
        raise
    if proc.returncode != 0:
        _audit_run(audit_caller, operation, "failure", error=f"exit {proc.returncode}")
    else:
        _audit_run(audit_caller, operation, "ok")
    return proc


_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_github_repo_url(link: str) -> tuple[str, str]:
    """Parse ``(owner, repo)`` from a full ``https://github.com/<owner>/<repo>`` URL.

    Deliberately strict (full URL only, per product decision — no bare
    ``owner/repo`` shorthand): rejects non-github.com hosts (SSRF guard) and
    constrains owner/repo to a safe charset before either value is ever
    interpolated into a subprocess argv.
    """
    if not link or not isinstance(link, str):
        raise RepoUrlError("repo link is empty")
    parsed = urlparse(link.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise RepoUrlError(
            f"not a github.com URL: {link!r} (expected https://github.com/<owner>/<repo>)"
        )
    parts = [p for p in (parsed.path or "").split("/") if p]
    if len(parts) < 2:
        raise RepoUrlError(f"not a full repo URL: {link!r} (expected .../<owner>/<repo>)")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if owner in (".", "..") or repo in (".", "..") or not (
        _SEGMENT_RE.match(owner) and _SEGMENT_RE.match(repo)
    ):
        raise RepoUrlError(f"invalid owner/repo segment in {link!r}")
    return owner, repo

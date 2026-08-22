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
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from kiro_crew import platform_compat, windows_acl

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
    for executable in ("gh", "glab", "az")
}

# Windows equivalents of the well-known dirs above, as the *subdirectory* each
# installer creates under a Program Files root. Expanded at call time rather
# than at import, because the roots come from the environment. These lead the
# ambient PATH for the same reason the POSIX list does: a machine-wide install
# is SYSTEM/Administrators-owned, so it should win over a user-writable shim
# that happens to sit earlier on PATH.
WINDOWS_PROVIDER_EXECUTABLE_SUBDIRS = {
    "gh": ("GitHub CLI",),
    "glab": ("GitLab CLI", "glab"),
}
WINDOWS_PROGRAM_ROOT_VARS = ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")

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
    """Apply the POSIX ownership/permission policy to one path component."""
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


def check_provider_path_component_windows(
    path: Path, *, label: str, me_sid: str, strict: bool
) -> None:
    """Apply the same policy to one path component, read from its Windows ACL.

    Same two questions as the POSIX walk — is this owned by a third account,
    and can anything outside the trusted set replace it — answered from the
    security descriptor because ``st_uid`` and the mode bits carry no
    information on Windows (see :mod:`kiro_crew.windows_acl`).

    *me_sid* is the gateway user's SID, the analog of ``uid``. Note that an
    administrator's own SID is covered by ``S-1-5-32-544`` regardless, exactly
    as POSIX trusts ``uid 0``.

    The component must also sit on a **local volume**, which arrives on the
    descriptor as ``volume_is_local`` rather than as a second platform call from
    here. ``WELL_KNOWN_TRUSTED_SIDS`` holds machine-local alias SIDs --
    ``S-1-5-18`` and ``S-1-5-32-544`` are the same string on every machine and
    denote a different principal on each -- so the descriptor of a file on a
    remote share names the FILE SERVER's SYSTEM and Administrators, and trusting
    them would mean "whoever administers that server may replace the binary this
    gateway executes". Both remote shapes are covered, including the mapped
    network drive (``Z:\\gh.exe``) that path inspection alone cannot tell from a
    local disk.

    Keeping that read inside :func:`windows_acl.describe` is what leaves this
    function a pure decision over one dataclass, so the policy stays testable on
    a non-Windows runner.
    """
    try:
        security = windows_acl.describe(path)
    except windows_acl.AclUnavailable as exc:
        # An unreadable ACL is a refusal: a trust check that cannot see the
        # descriptor has not cleared anything.
        raise ValueError(f"{label} security descriptor is unreadable: {exc}") from exc

    if not security.volume_is_local:
        raise ValueError(
            f"{label} is not on a local volume; the trust policy's well-known SIDs "
            "are machine-local and carry no meaning off this host"
        )

    if security.null_dacl:
        raise ValueError(f"{label} has a NULL DACL, which grants everyone full control")
    if security.unparsable_ace_types:
        types = ",".join(str(t) for t in security.unparsable_ace_types)
        raise ValueError(f"{label} carries ACE types this policy cannot evaluate (type {types})")

    trusted = set(windows_acl.WELL_KNOWN_TRUSTED_SIDS)
    if strict:
        # Strict mode is the analog of "root-owned and unwritable by the
        # gateway user": the machine, not the user, must own and control it.
        if security.owner_sid not in trusted:
            raise ValueError(
                f"{label} is not owned by the system "
                f"(owner {security.owner_name}, {security.owner_sid})"
            )
    else:
        trusted.add(me_sid)
        if security.owner_sid not in trusted:
            raise ValueError(
                f"{label} is owned by another account "
                f"({security.owner_name}, {security.owner_sid})"
            )

    offenders = [writer for writer in security.writers if writer.sid not in trusted]
    if offenders:
        joined = "; ".join(writer.describe() for writer in offenders)
        raise ValueError(f"{label} can be replaced by {joined}")


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

    On **Windows** the same two questions are answered from the object's ACL
    rather than from ``st_uid`` and the mode bits, which carry no information
    there (see :mod:`kiro_crew.windows_acl`). An **elevated** gateway is refused
    for the same reason a root one is: its children would be elevated too.
    """
    if not os.path.isabs(candidate):
        raise ValueError("path must be absolute")

    windows = sys.platform == "win32"
    uid = -1
    me_sid = ""
    if windows:
        # Both of these live in platform_compat because it already owns "read
        # this process's own access token" for the codebase. Both are tri-state
        # and BOTH non-True answers refuse: an unreadable token is not a
        # not-elevated token, and an unverifiable SID is not a trusted one.
        elevated = platform_compat.is_token_elevated()
        if elevated is None:
            raise ValueError("provider execution is disabled: the gateway token is unreadable")
        if elevated:
            raise ValueError("provider execution is disabled for an elevated gateway")
        me_sid = platform_compat.current_user_sid() or ""
        if not me_sid:
            raise ValueError("provider execution is disabled: the gateway user's SID is unverifiable")
    else:
        getuid = getattr(os, "getuid", None)
        geteuid = getattr(os, "geteuid", getuid)
        if getuid is None or geteuid is None:
            raise ValueError("filesystem ownership checks are unavailable")
        if geteuid() == 0:
            raise ValueError("provider execution is disabled for a root gateway")
        uid = geteuid()
    strict = strict_provider_bins()

    def _check(target: Path, *, label: str) -> None:
        """Dispatch one component to the platform's ownership policy."""
        if windows:
            check_provider_path_component_windows(target, label=label, me_sid=me_sid, strict=strict)
        else:
            check_provider_path_component(target, label=label, uid=uid, strict=strict)

    original = Path(candidate)
    try:
        resolved = original.resolve(strict=True)
    except OSError as exc:
        raise ValueError("path does not exist") from exc
    # Windows paths are case-insensitive and ``resolve()`` rewrites a component
    # to its on-disk casing, so a candidate spelled `gh.exe` against a file
    # named `gh.EXE` differs from its resolution without any symlink being
    # involved. Comparing case-sensitively there would refuse a plain install
    # in strict mode and pointlessly re-walk the same parents in relaxed mode.
    same_path = (
        original.as_posix().casefold() == resolved.as_posix().casefold()
        if windows
        else original == resolved
    )
    if strict and not same_path:
        raise ValueError("path must be canonical and contain no symlinks")

    try:
        if not stat.S_ISREG(resolved.stat().st_mode):
            raise ValueError("path is not a regular file")
    except OSError as exc:
        raise ValueError("executable hierarchy is not accessible") from exc
    # On Windows this is close to an existence test (the OS has no execute
    # bit), so it is a coherence check there rather than part of the trust policy.
    if not os.access(resolved, os.X_OK):
        raise ValueError("file is not executable")

    if not strict:
        for root in agent_writable_roots():
            if resolved == root or root in resolved.parents:
                raise ValueError(f"executable is inside the agent-writable tree {root}")

    _check(resolved, label="executable")
    # A symlink's own directory chain is part of the provenance too (relaxed
    # mode allows symlinks, so /opt/homebrew/bin gets checked as well).
    parents = list(path_parents(resolved))
    if not strict and not same_path:
        parents += [p for p in path_parents(original) if p not in parents]
    for parent in parents:
        try:
            if not stat.S_ISDIR(parent.stat().st_mode):
                raise ValueError("executable parent is not a directory")
        except OSError as exc:
            raise ValueError("executable hierarchy is not accessible") from exc
        _check(parent, label="executable parent")
    return str(resolved)


def _wellknown_windows_dirs(executable: str) -> tuple[str, ...]:
    """Directories a machine-wide Windows install of *executable* lives in.

    Expanded at call time rather than at import, because the Program Files
    roots come from the environment.
    """
    if sys.platform != "win32":
        return ()
    dirs: list[str] = []
    for variable in WINDOWS_PROGRAM_ROOT_VARS:
        root = os.environ.get(variable)
        if not root:
            continue
        for subdir in WINDOWS_PROVIDER_EXECUTABLE_SUBDIRS.get(executable, ()):
            dirs.append(os.path.join(root, subdir))
            dirs.append(os.path.join(root, subdir, "bin"))
    return tuple(dict.fromkeys(dirs))


def provider_executable_candidates(executable: str) -> tuple[str, ...]:
    """Absolute paths to try for *executable*, in resolution order.

    The well-known install dirs come first (a managed root-owned copy still
    wins when one exists), then every ``PATH`` hit — so the install the user
    already runs from their terminal is found even when it lives somewhere this
    module has never heard of (asdf, mise, ``~/.local/bin``). ``PATH`` is not
    consulted in strict mode, which by definition only trusts system dirs.

    Resolution inside a directory is delegated to :func:`shutil.which`, which
    applies whatever the platform defines as "runnable there": ``PATHEXT`` on
    Windows, so a bare ``gh`` matches ``gh.exe``, and ``X_OK`` on POSIX. Joining
    the bare name by hand is why this scan previously found nothing at all on
    Windows.

    A hit is then required to actually LIE INSIDE the directory that was asked
    for, because on Windows ``which`` does not only search ``path``::

        if sys.platform == "win32":
            # The current directory takes precedence on Windows.
            ...
            path.insert(0, curdir)

    -- so `which(name, path=directory)` searches the process CWD *first*, and a
    checkout that happens to contain a `gh.exe` would win over every well-known
    install dir. Verified: with an attacker copy in the CWD, that call returns
    `.\\gh.exe`. Since the gateway's CWD is not something this module controls,
    the containment check is what makes delegating to ``which`` safe.

    The containment test uses ``abspath``, deliberately not ``resolve``: the
    question here is only "did this come from the directory I asked for", while
    where a symlink ultimately points is the trust walk's job -- and that walk
    resolves and re-checks every component with its own policy.
    """

    def _inside(candidate: str, directory: str) -> bool:
        """True when *candidate* resolves to a file directly in *directory*."""
        base = os.path.normcase(os.path.abspath(directory)).rstrip(os.sep)
        target = os.path.normcase(os.path.abspath(candidate))
        return os.path.dirname(target) == base

    ordered: dict[str, None] = dict.fromkeys(PROVIDER_EXECUTABLE_CANDIDATES.get(executable, ()))
    searched = list(_wellknown_windows_dirs(executable))
    if not strict_provider_bins():
        searched += [e for e in (os.environ.get("PATH") or "").split(os.pathsep) if e]
    for directory in searched:
        found = shutil.which(executable, path=directory)
        if found and _inside(found, directory):
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
    """
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
        # Deliberately BYTES here (no `text=True`), then decoded below.
        #
        # `text=True` alone decodes with the LOCALE encoding, which is the ANSI
        # codepage on Windows -- so a non-ASCII issue title crashed this call.
        # Verified on a cp936 host: `UnicodeDecodeError: 'gbk' codec can't decode
        # byte 0xac`, raised inside subprocess's own reader THREAD, so `run`
        # returns with `stdout=None` and the caller dies on the None rather than
        # on a decode error it could attribute. Not Windows-only in principle --
        # any non-UTF-8 locale does it.
        #
        # `encoding="utf-8"` would fix the codec but not the attribution: a
        # strict failure still dies in that reader thread, and `errors="replace"`
        # instead lets U+FFFD through into a JSON *string value*, where the JSON
        # stays syntactically valid, `json.loads` succeeds, and the replacement
        # character reaches stored issue records. Decoding here keeps both
        # properties: strict, so nothing is silently corrupted, and in our own
        # frame, so a failure is attributable and carries no payload bytes.
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            timeout=timeout,
            check=False,
            input=input_text.encode("utf-8") if input_text is not None else None,
            env=gh_env(pin_host=pin_host),
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
    try:
        decoded = subprocess.CompletedProcess(
            proc.args,
            proc.returncode,
            stdout=proc.stdout.decode("utf-8") if proc.stdout is not None else None,
            stderr=proc.stderr.decode("utf-8") if proc.stderr is not None else None,
        )
    except UnicodeDecodeError as exc:
        # gh emits UTF-8, so this is a genuine anomaly rather than a locale
        # mismatch. Audit and raise with the stream and offset only -- never the
        # offending bytes, which are provider payload.
        _audit_run(audit_caller, operation, "failure", error="undecodable output")
        raise SetupError(
            f"gh returned output that is not valid UTF-8 (at byte {exc.start})"
        ) from exc
    if decoded.returncode != 0:
        _audit_run(audit_caller, operation, "failure", error=f"exit {decoded.returncode}")
    else:
        _audit_run(audit_caller, operation, "ok")
    return decoded


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

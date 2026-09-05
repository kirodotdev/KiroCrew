"""Validate a GitHub repo URL, clone it with push DISABLED, enumerate its branches.

This is the front door of a run: the user names a repository here, and the
push-disable performed at clone time is the app's #1 safety control — the spine
refuses to run against a clone whose push remote is live.

Ported from the upstream module, GitHub-only: the internal-host allowlist entry,
the internal SSH URL construction, and the CloudFarm code path are removed. Only
`github.com` is accepted, and the clone URL is rebuilt from validated
owner/repo components (never raw user text) so it is safe as a single git argv
element.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from kiro_crew.platform.context import redact_via_context
from kiro_crew.platform_compat import (
    first_linked_ancestor,
    is_link_or_junction,
    rmtree_force,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

from ..spine.git_safety import GIT_SAFE_CONFIG, require_pinned

#: Alias the ONE shared safe-config so this helper cannot drift from the others (the
#: structural test in `test_dogfood_learnings` asserts identity). See `backend/commit.py`.
_GIT_SAFE_CONFIG = GIT_SAFE_CONFIG

logger = logging.getLogger(__name__)

#: Allowlist, never a denylist (defense in depth for SSRF). GitHub only.
_ALLOWED_HOSTS = frozenset({"github.com", "www.github.com"})

#: https://github.com/<owner>/<repo>[.git][/...]
_GITHUB_RE = re.compile(
    r"^https://(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9._-]{1,100})"
    r"/(?P<repo>[A-Za-z0-9._-]{1,100}?)(?:\.git)?(?:/.*)?$"
)
_MAX_URL_LEN = 400

#: The cross-cutting push-disable sentinel — matches the spine's isolation check.
DISABLED_NO_PUSH = "DISABLED_NO_PUSH"

_GIT_REPOSITORY_ENV_KEYS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_SYSTEM",
        "GIT_DIR",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_TEMPLATE_DIR",
        "GIT_WORK_TREE",
    }
)


def _git_env(*, network_protocol: str = "") -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _GIT_REPOSITORY_ENV_KEYS
        and not key.startswith("GIT_CONFIG_KEY_")
        and not key.startswith("GIT_CONFIG_VALUE_")
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ALLOW_PROTOCOL": network_protocol,
        }
    )
    if not network_protocol:
        env.update(
            {
                "GIT_PROTOCOL_FROM_USER": "0",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
            }
        )
    return env


class IsolationProbeError(RuntimeError):
    """The push-isolation probe COULD NOT RUN — its sandbox launcher failed.

    Raised instead of the fail-closed ``False`` because the two nonzero exits
    mean opposite things: a probe that RAN and found a live url is a repository
    problem ("re-run repository setup" fixes it), while a probe whose launcher
    died before ``git`` executed says nothing about the remotes and setup
    cannot fix it. Subclasses ``RuntimeError`` so the run-start route's
    existing handler surfaces the message verbatim instead of a 500.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            "the push-isolation probe could not run: the sandbox launcher "
            "failed before git executed"
            + (f" ({detail})" if detail else "")
            + " — the clone's remote urls were never read, so this is a "
            "sandbox failure, not a live push url; fix the sandbox (see the "
            "gateway log) rather than re-running repository setup"
        )
        self.detail = detail


#: Signatures only the namespace-sandbox launcher emits on stderr, matched
#: STRUCTURALLY so repository-influenced text cannot satisfy them (raised by
#: the Opus review of this branch): a repo may legally be NAMED
#: ``kirocrew_sandbox_x`` (``_GITHUB_RE`` admits ``_``), which puts that
#: substring into the clone PATH that git echoes on path-printing fatals — so
#: the script-filename marker only counts inside a real Python traceback frame
#: (``File "…kirocrew_sandbox_….py"`` plus the ``Traceback`` banner, a shape
#: git never prints), and the launcher's deliberate refusal prefixes must
#: START a stderr line (owner/repo names cannot contain a space or colon, so
#: no echoed path can begin a line with ``sandbox: ``). ``sandbox: WARNING``
#: is deliberately NOT classified: the launcher warns and then still runs the
#: command, so a warning can coexist with git's own exit code and must not
#: reclassify it. The prefixes are pinned against the generated launcher by a
#: round-trip test so this list cannot drift silently.
_LAUNCHER_EXIT_PREFIXES = (
    "sandbox: BLOCKED",
    "sandbox: FATAL",
    "sandbox: unshare(",
    "sandbox_launcher:",
)
_LAUNCHER_TRACEBACK_RE = re.compile(r'^\s*File "[^"\n]*kirocrew_sandbox_[^"\n]*\.py"', re.MULTILINE)


def _launcher_failure_detail(stderr: str) -> str | None:
    """The bounded, redacted detail line when *stderr* shows the sandbox
    launcher itself failed, else ``None`` (the exit code is git's own)."""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    launcher_failed = any(line.startswith(_LAUNCHER_EXIT_PREFIXES) for line in lines) or (
        "Traceback (most recent call last)" in stderr
        and _LAUNCHER_TRACEBACK_RE.search(stderr) is not None
    )
    if not launcher_failed:
        return None
    # The surfaced message carries only a bounded tail; the full (redacted,
    # bounded) stderr goes to the log here, at the one classification site, so
    # "see the gateway log" in the raised message is a promise that is kept.
    logger.error(
        "push-isolation probe could not run — sandbox launcher stderr (redacted): %s",
        redact_via_context(stderr.strip())[:2000],
    )
    # The last line is the significant one for both failure shapes: a Python
    # traceback ends with the exception ("ModuleNotFoundError: ..."), and the
    # launcher's own sys.exit messages lead with their prefix.
    tail = lines[-1] if lines else ""
    # Redact BEFORE the bound, same as every stderr surface in this module.
    return redact_via_context(tail)[:200]


def _origin_urls(repo: Path, *, push: bool) -> list[str] | None:
    """Read origin's fetch/push urls from the clone's local config, as data.

    Returns the url list, ``[]`` when the key is absent, or ``None`` for an
    ambiguous git failure (callers fail closed on ``None``). Raises
    :class:`IsolationProbeError` for the one nonzero exit that is NOT evidence
    about the remotes at all: a namespace-sandbox launcher dying before git
    executed, identified by the launcher's own stderr signature. This module
    spawns git directly, so no launcher exists in this probe's chain on a
    stock install — the signature appears only on deployments that route the
    gateway's subprocesses through the sandbox (the issue #8151 host, where
    every ``git remote get-url`` probe carried the launcher's traceback), and
    the classification is inert everywhere else because the structural
    matching in :func:`_launcher_failure_detail` cannot be satisfied by git's
    own output. Collapsing that crash into the fail-closed path reported
    "push is not disabled" for a clone whose remotes were never read — the
    misleading 409 in issue #8151.
    """
    key = "remote.origin.pushurl" if push else "remote.origin.url"
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                *_GIT_SAFE_CONFIG,
                "config",
                "--local",
                "--no-includes",
                "--get-all",
                key,
            ],
            capture_output=True,
            timeout=30,
            shell=False,
            env=_git_env(),
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        detail = _launcher_failure_detail(proc.stderr or "")
        if detail is not None:
            raise IsolationProbeError(detail)
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        return None
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _repository_is_safe(repo: Path) -> bool:
    """True iff the clone's Git metadata and local config are safe to reuse.

    Fails CLOSED (``False``) for ambiguous git errors and for any unsafe
    filesystem shape, but raises :class:`IsolationProbeError` when the
    unsafe-keys probe's sandbox launcher died before git executed — a crashed
    launcher exits 1, indistinguishable from git's own "no unsafe keys", so
    reading the exit code alone would report an unscanned config as safe (the
    one probe in the isolation chain that failed OPEN, issue #8493).
    """
    git_dir = repo / ".git"
    if first_linked_ancestor(git_dir) or is_link_or_junction(git_dir):
        return False
    if not git_dir.is_dir():
        return False
    for directory in (
        git_dir / "objects",
        git_dir / "objects" / "info",
        git_dir / "info",
    ):
        if first_linked_ancestor(directory) or is_link_or_junction(directory):
            return False
    for forbidden in (
        git_dir / "commondir",
        git_dir / "config.worktree",
        git_dir / "objects" / "info" / "alternates",
        git_dir / "objects" / "info" / "http-alternates",
        git_dir / "info" / "grafts",
    ):
        if os.path.lexists(forbidden):
            return False
    object_dir = git_dir / "objects"
    errors: list[OSError] = []
    for current, directories, files in os.walk(
        git_dir, topdown=True, followlinks=False, onerror=errors.append
    ):
        current_path = Path(current)
        for name in directories:
            if is_link_or_junction(current_path / name):
                return False
        for name in files:
            path = current_path / name
            if is_link_or_junction(path):
                return False
            try:
                metadata = path.lstat()
            except OSError:
                return False
            if not stat.S_ISREG(metadata.st_mode):
                return False
            if not path.is_relative_to(object_dir) and metadata.st_nlink != 1:
                return False
    if errors:
        return False

    unsafe_keys = (
        r"^(include\.path|includeif\..*\.path|"
        r"url\..*\.(insteadof|pushinsteadof)|"
        r"credential\..*|"
        r"core\.(alternaterefscommand|askpass|attributesfile|editor|excludesfile|fsmonitor|"
        r"gitproxy|hookspath|pager|sshcommand|worktree)|"
        r"diff\..*|difftool\..*|"
        r"filter\..*\.(clean|process|smudge)|gpg\..*|"
        r"merge\..*\.driver|mergetool\..*|sequence\.editor|ssh\..*|"
        r"https?\..*|protocol\..*|"
        r"remote\..*\.(proxy|receivepack|uploadpack)|"
        r"extensions\.worktreeconfig)$"
    )
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                *_GIT_SAFE_CONFIG,
                "config",
                "--local",
                "--no-includes",
                "--name-only",
                "--get-regexp",
                unsafe_keys,
            ],
            capture_output=True,
            timeout=30,
            shell=False,
            env=_git_env(),
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        # Exit 1 means "no unsafe keys" only when git itself ran. A sandbox
        # launcher that dies before git executes also exits 1, so reading the
        # exit code alone makes this the one probe in the isolation chain that
        # fails OPEN during a launcher outage (issue #8493): metadata unsafety
        # becomes invisible exactly when the host cannot run the probes. Same
        # classifier as :func:`_origin_urls` — the structural stderr match is
        # what keeps git's own output unable to satisfy it, and the raise says
        # "the probe could not run" instead of an isolation verdict (#8151).
        detail = _launcher_failure_detail(proc.stderr or "")
        if detail is not None:
            raise IsolationProbeError(detail)
    return proc.returncode == 1


_UNSAFE_CLONE_RETENTION = 3


def _prune_unsafe_clone_retirements(parent: Path, repo_name: str, current: Path) -> None:
    """Keep the current incident plus the two newest prior retired clones."""
    prefix = f".{repo_name}.unsafe-"
    try:
        candidates = [
            path
            for path in parent.iterdir()
            if path.name.startswith(prefix)
            and path != current
            and not is_link_or_junction(path)
            and path.is_dir()
        ]
        candidates.sort(key=lambda path: path.lstat().st_mtime_ns, reverse=True)
    except OSError:
        logger.warning("could not inventory retired clones under %s", parent)
        return
    for stale in candidates[_UNSAFE_CLONE_RETENTION - 1 :]:
        if not rmtree_force(stale):
            logger.warning("could not prune retired clone %s", stale)


def _retire_unsafe_clone(repo: Path) -> Path | None:
    """Atomically remove an unsafe clone from its canonical name without Git.

    This is an incident boundary, not setup recovery: it runs only when a clone that
    was safe before an agent/build step fails validation afterwards. Renaming the root
    directory does not dereference the now-hostile Git metadata, prevents a later run
    from adopting a rejected provisional commit, and preserves bytes for diagnosis.
    """
    repo = Path(repo)
    parent = repo.parent
    if (
        first_linked_ancestor(parent)
        or is_link_or_junction(parent)
        or is_link_or_junction(repo)
        or not repo.is_dir()
    ):
        return None
    container = parent / f".{repo.name}.unsafe-{secrets.token_hex(8)}"
    retired = container / repo.name
    try:
        if os.name == "nt":
            # mkdir is the no-replace reservation. Windows rename cannot replace a
            # non-empty destination and never follows a destination symlink.
            container.mkdir(mode=0o700)
            os.rename(repo, retired)
        else:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            parent_fd = os.open(parent, flags)
            container_fd = -1
            try:
                os.mkdir(container.name, mode=0o700, dir_fd=parent_fd)
                container_fd = os.open(container.name, flags, dir_fd=parent_fd)
                # Both names are descriptor-relative. If a same-UID racer creates
                # `retired` first, rename either replaces only that directory entry
                # (never its target) or fails on a non-empty directory.
                os.rename(
                    repo.name,
                    repo.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=container_fd,
                )
            finally:
                if container_fd >= 0:
                    os.close(container_fd)
                os.close(parent_fd)
    except OSError:
        logger.exception("could not retire unsafe clone %s", repo)
        try:
            container.rmdir()
        except OSError:
            pass
        return None
    _prune_unsafe_clone_retirements(parent, repo.name, container)
    return retired


def _push_disabled(repo: Path) -> bool:
    return _origin_urls(repo, push=True) == [DISABLED_NO_PUSH] and _origin_urls(
        repo, push=False
    ) == [DISABLED_NO_PUSH]


def _repository_is_isolated(repo: Path) -> bool:
    """True only when metadata/config is safe and every origin URL is disabled.

    Fails CLOSED (``False``) for ambiguous git errors, but raises
    :class:`IsolationProbeError` when the probe's sandbox launcher crashed
    before git executed — that exit is not evidence about the remotes, and
    reporting it as "push is not disabled" hid the real failure (#8151). Both
    outcomes refuse to start; only the surfaced reason differs.
    """
    return _repository_is_safe(repo) and _push_disabled(repo)


@dataclass
class CloneSpec:
    """The validated, derived clone target, built only from validated components."""

    display: str  # human label: owner/repo
    clone_url: str  # the https URL git clones FROM
    dir_name: str  # local dir name under scratch


def _gh_prefers_ssh() -> bool:
    """True iff the ``gh`` CLI uses SSH for git against github.com.

    The host-scoped setting is checked first because it is commonly per-host: on
    the dev host this was written against, the global default is ``https`` while
    github.com is explicitly ``ssh``, and reading only the global value would pick
    a transport that cannot authenticate a private clone.
    """
    if shutil.which("gh") is None:
        return False
    for args in (
        ["gh", "config", "get", "git_protocol", "-h", "github.com"],
        ["gh", "config", "get", "git_protocol"],
    ):
        try:
            proc = subprocess.run(args, capture_output=True, timeout=15, **UTF8_TEXT)
        except (OSError, subprocess.SubprocessError):
            return False
        value = (proc.stdout or "").strip().lower()
        if proc.returncode == 0 and value:
            return value == "ssh"
    return False


def _host_is_blocked(host: str) -> bool:
    """SSRF defense-in-depth: refuse a host that resolves to a private/loopback/
    metadata address, even though the allowlist already excludes it by name. Guards
    against an allowlisted name being pointed at an internal address (DNS rebinding).
    Fail closed on any resolution error."""
    if not host:
        return True
    low = host.lower()
    if low in {"localhost", "metadata.google.internal"}:
        return True
    if low in {"169.254.169.254", "fd00:ec2::254"}:  # cloud metadata
        return True
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_unspecified
            ):
                return True
    except (OSError, ValueError):
        return True
    return False


def validate_target_url(url: str) -> tuple[CloneSpec | None, str]:
    """Validate a user-supplied GitHub URL. Returns ``(CloneSpec, "")`` or
    ``(None, reason)``. Pure validation — the only I/O is a read-only DNS resolve."""
    if not isinstance(url, str) or not url.strip():
        return None, "Enter a GitHub repository URL."
    url = url.strip()
    if len(url) > _MAX_URL_LEN:
        return None, "URL is too long."

    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None, "Only https:// GitHub URLs are supported."
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        return None, f"Only github.com URLs are supported. Got host: {host or '<none>'}"
    if _host_is_blocked(host):
        return None, "URL host is not allowed (blocked address)."

    match = _GITHUB_RE.match(url)
    if not match:
        return None, "URL did not match a github.com/<owner>/<repo> URL."
    owner, repo = match.group("owner"), match.group("repo")
    # Clone over whichever transport is actually authenticated. HTTPS is the
    # natural default, but a PRIVATE repo needs credentials — and on a host where
    # git's credential helper points elsewhere and ``gh`` is configured for SSH
    # (observed here: the HTTPS clone died with "could not read Username"), only
    # SSH authenticates. The owner/repo still come from the validated match, so
    # the transport swap cannot retarget the clone.
    if _gh_prefers_ssh():
        clone_url = f"git@github.com:{owner}/{repo}.git"
    else:
        clone_url = f"https://github.com/{owner}/{repo}.git"
    return (
        CloneSpec(
            display=f"{owner}/{repo}",
            clone_url=clone_url,
            dir_name=f"{owner}--{repo}",
        ),
        "",
    )


def setup_safe_clone(url: str, scratch_root: Path, *, timeout_s: int = 300) -> tuple[dict, str]:
    """Public entry: clone (or reuse) with push disabled. Returns ``(result, err)``.

    A probe whose sandbox launcher crashed surfaces as the error string, not as
    an exception: this function's callers consume ``(result, err)`` tuples off
    a worker thread, and a raise here would turn a diagnosable sandbox failure
    into a 500. The clone (when one exists) is deliberately left in place —
    its remotes were never read, so there is no isolation verdict to act on,
    and deleting a good clone over an unrelated sandbox failure only forces a
    re-download after the sandbox is fixed.
    """
    try:
        return _setup_safe_clone(url, scratch_root, timeout_s=timeout_s)
    except IsolationProbeError as exc:
        return {}, str(exc)


def _setup_safe_clone(url: str, scratch_root: Path, *, timeout_s: int = 300) -> tuple[dict, str]:
    """Validate and install/reuse the canonical push-disabled clone.

    Reuse attests only enforceable properties: canonical location, safe Git
    metadata/config, and exactly one disabled fetch/push URL. Clone contents stay
    agent-writable by design and are never represented as cryptographically trusted.
    """
    spec, err = validate_target_url(url)
    if not spec:
        return {}, err
    if is_link_or_junction(scratch_root) or first_linked_ancestor(scratch_root):
        return {}, "Clone scratch directory is under a link or junction (refused for safety)."

    scratch_root.mkdir(parents=True, exist_ok=True)
    if is_link_or_junction(scratch_root) or first_linked_ancestor(scratch_root):
        return {}, "Clone scratch directory failed safety verification."
    dest = scratch_root / spec.dir_name

    if is_link_or_junction(dest):
        return {}, f"Destination is a link or junction (refused for safety): {dest}"

    git_dir = dest / ".git"
    if is_link_or_junction(git_dir):
        return {}, f"Existing clone at {dest} has a linked Git directory — refusing reuse."
    if git_dir.is_dir():
        if not _repository_is_safe(dest):
            return {}, f"Existing clone at {dest} failed Git metadata safety verification."
        origins = _origin_urls(dest, push=False)
        if origins is None or len(origins) != 1:
            return {}, f"Existing clone at {dest} has ambiguous origin URLs — refusing reuse."
        actual_origin = origins[0]
        if actual_origin not in {DISABLED_NO_PUSH, spec.clone_url}:
            return {}, (
                f"Existing clone at {dest} has origin {actual_origin!r}, which does not "
                f"match the requested {spec.clone_url!r} — refusing to reuse it."
            )
        _disable_push(dest)
        result = _ok(spec, dest, reused=True)
        if not result.get("push_disabled"):
            return {}, "clone push could not be disabled — refusing reuse"
        return result, ""

    if dest.exists():
        return {}, f"Destination already exists and is not a git repo: {dest}"

    protocol = "ssh" if spec.clone_url.startswith("git@") else urlparse(spec.clone_url).scheme
    if protocol not in {"file", "https", "ssh"}:
        return {}, "validated clone URL has no supported transport"
    try:
        proc = subprocess.run(
            [
                "git",
                *_GIT_SAFE_CONFIG,
                "-c",
                "credential.helper=!gh auth git-credential",
                "clone",
                "--origin",
                "origin",
                spec.clone_url,
                str(dest),
            ],
            capture_output=True,
            timeout=timeout_s,
            shell=False,
            env=_git_env(network_protocol=protocol),
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired:
        rmtree_force(dest)
        return {}, f"git clone timed out after {timeout_s}s."
    except OSError as exc:
        # No child started, so this setup attempt cannot own anything at `dest`.
        # Do not race-delete a path another same-UID process may have created.
        return {}, f"git clone could not start: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        rmtree_force(dest)
        # Redact BEFORE the bound (here and at every sibling site below): the slice
        # can cut a credential in the echoed remote URL mid-match, leaving a fragment
        # no downstream redaction pass recognises.
        return {}, f"git clone failed: {redact_via_context(tail[0])[:200]}"
    try:
        safe = _repository_is_safe(dest)
    except IsolationProbeError:
        # This attempt created `dest` and has not disabled push yet, so unlike
        # the reuse path there is no good clone to preserve — remove it rather
        # than leaving a live origin url at the canonical location.
        rmtree_force(dest)
        raise
    if not safe:
        rmtree_force(dest)
        return {}, "cloned repository failed Git metadata safety verification"

    _disable_push(dest)
    result = _ok(spec, dest, reused=False)
    if not result.get("push_disabled"):
        rmtree_force(dest)
        return {}, "clone push could not be disabled — refusing"
    return result, ""


#: Shape check for a user-selected branch: allowlisted charset, no leading dash
#: (option injection), no ``..``/``@{`` ref sequences, no segment starting/ending
#: with a dot. Callers additionally require the name to be in the clone's own
#: enumerated set, so an unknown ref is rejected even when well-shaped.
_BRANCH_NAME_RE = re.compile(
    r"^(?!-)(?!.*\.\.)(?!.*@\{)(?!.*(?:^|/)\.)(?!.*\.(?:/|$))[A-Za-z0-9._/-]{1,200}$"
)


def is_valid_branch_name(name: str) -> bool:
    """True iff ``name`` is a safe git branch ref token (shape check)."""
    return bool(
        isinstance(name, str)
        and name
        and _BRANCH_NAME_RE.match(name)
        and not name.endswith("/")
        and not name.endswith(".lock")
    )


def list_clone_branches(clone: Path, *, timeout_s: int = 30) -> tuple[list[str], str]:
    """Enumerate an existing clone's branches, default/HEAD first. Read-only, no
    network fetch, operates only on the server-controlled clone dir."""
    clone = Path(clone)
    if not (clone / ".git").is_dir():
        return [], f"Not a git clone: {clone}"
    try:
        if not _repository_is_safe(clone):
            return [], "clone Git metadata failed safety verification"
        disabled = _push_disabled(clone)
    except IsolationProbeError as exc:
        return [], str(exc)
    if not disabled:
        return [], "clone is not push-disabled"
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/remotes/origin",
            "refs/heads",
        ],
        capture_output=True,
        timeout=timeout_s,
        shell=False,
        env=_git_env(),
        **UTF8_TEXT,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return [], f"could not list branches: {redact_via_context(tail[0])[:160]}"
    names: list[str] = []
    seen: set[str] = set()
    for raw in (proc.stdout or "").splitlines():
        b = raw.strip()
        if not b or b.endswith("/HEAD"):
            continue
        # Skip the spine's own throwaway candidate branches and the bare origin ref.
        if b == "origin" or b.startswith("cand/") or "/cand/" in b:
            continue
        if b in seen or not is_valid_branch_name(b):
            continue
        seen.add(b)
        names.append(b)
    if not names:
        return [], "no branches found in the clone"
    head = subprocess.run(
        ["git", "-C", str(clone), "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"],
        capture_output=True,
        timeout=timeout_s,
        shell=False,
        env=_git_env(),
        **UTF8_TEXT,
    )
    default = (head.stdout or "").strip()
    ordered = ([default] if default in names else []) + sorted(n for n in names if n != default)
    return ordered, ""


#: Network hosts this app may push to. Mirrors the host `validate_target_url` accepts for the
#: SETUP url, applied to the STORED value and expressed over both shapes `setup_safe_clone`
#: can persist (https and scp-like ssh). Kept as a set so a GitHub Enterprise host can be
#: added in one place if this app ever supports one.
_ALLOWED_REMOTE_HOSTS = frozenset({"github.com"})


def _is_allowed_remote(url: str) -> bool:
    """Whether a stored ``origin_url`` is safe to use as a push destination.

    ``resolve_origin_url`` cannot simply re-run :func:`validate_target_url` here: that helper
    accepts only ``https://`` INPUT, while :func:`setup_safe_clone` writes ``spec.clone_url``
    — the SSH form ``git@github.com:owner/repo.git`` whenever ``gh`` prefers ssh. Re-validating
    would have refused every ssh-configured install's own remote and silently degraded it to
    queue-only. (Found by measuring both shapes instead of assuming they were interchangeable.)

    The rule is therefore about the NETWORK HOST, which is the property that matters: a
    tampered config must not be able to redirect a push to a host the operator never chose.

    * A remote NETWORK url must be on the allowlist — exact host match, not ``endswith``, so
      ``evilgithub.com`` and ``github.com.attacker.net`` both fail.
    * A LOCAL path (``/tmp/x.git``, ``file://``, or a relative path) is allowed: it cannot
      exfiltrate anywhere, it is what the app's own tests push to, and an operator pointing at
      a local bare repo is a legitimate offline setup.
    * The ``DISABLED_NO_PUSH`` sentinel is refused — it is a marker, not a destination.
    """
    raw = (url or "").strip()
    if not raw or raw == DISABLED_NO_PUSH:
        return False
    if raw.startswith("git@"):
        # scp-like syntax: git@HOST:owner/repo(.git)
        host, sep, path = raw[len("git@") :].partition(":")
        return bool(sep) and host.lower() in _ALLOWED_REMOTE_HOSTS and bool(path.strip("/"))
    parsed = urlparse(raw)
    if parsed.scheme in ("", "file"):
        # No network host to redirect to.
        return bool((parsed.path or raw).strip())
    if parsed.scheme not in ("https", "ssh"):
        # http:// (cleartext), git://, ftp://, … are never our push transport.
        return False
    # `hostname` strips any userinfo (`x-access-token:TOK@host`) and port.
    return (parsed.hostname or "").lower() in _ALLOWED_REMOTE_HOSTS and bool(parsed.path.strip("/"))


def _remote_slug(url: str) -> str:
    """``owner/repo`` (lower-cased, no ``.git``) for a remote url, or ``""``.

    Transport-agnostic on purpose: ``setup_safe_clone`` stores whichever form ``gh`` is
    authenticated for, so `git@github.com:o/r.git` and `https://github.com/o/r.git` must
    compare EQUAL. Returns ``""`` for a local path (nothing to compare — a local bare repo
    cannot exfiltrate, which is why the caller allows it outright).
    """
    raw = (url or "").strip()
    if not raw or raw == DISABLED_NO_PUSH:
        return ""
    if raw.startswith("git@"):
        _host, sep, path = raw[len("git@") :].partition(":")
        if not sep:
            return ""
    else:
        parsed = urlparse(raw)
        if parsed.scheme in ("", "file") or not parsed.hostname:
            return ""  # local path: no identity to pin
        path = parsed.path
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return f"{owner.lower()}/{repo.lower()}"


def resolve_origin_url(config: dict) -> str:
    """The real remote for a trusted publisher, or ``""``.

    Prefers ``origin_url`` (written by :func:`setup_safe_clone`). Falls back to
    re-VALIDATING the retained ``target_url`` so a config written before both remote urls
    were neutralized keeps working without a re-setup — otherwise every existing install
    would silently degrade to queue-only after upgrading.

    BOTH keys are re-run through :func:`validate_target_url` rather than trusted as stored:
    that rebuilds the clone url from validated components (host allowlisted to github.com),
    so a hand-edited ``config.json`` cannot smuggle in an arbitrary push destination.
    Returns ``""`` when it does not validate, and every caller treats ``""`` as "no push
    target" — fail closed.

    ``origin_url`` used to be returned VERBATIM while only the legacy fallback validated,
    which made the docstring's own promise false for the preferred path. Measured:
    ``{"origin_url": "https://attacker.example.com/exfil.git"}`` was returned unchanged and
    became the push destination, while the identical string under ``target_url`` was
    correctly refused ("Only github.com URLs are supported"). This is the one place the push
    destination is resolved for the draft-PR push, the F10 direct push and one-click commit,
    so an unvalidated value here redirects all three. Raised by the GPT review of this branch;
    the security guidance on untrusted URL destinations asks for exactly this — allowlist the
    destination rather than trusting persisted input.
    """
    direct = str((config or {}).get("origin_url") or "").strip()
    if direct:
        if not _is_allowed_remote(direct):
            logger.warning("stored origin_url is not an allowed remote — no push target")
            return ""
        # HOST-allowlisting alone is not enough: `github.com` is an allowed host, so an
        # injected `config.json` could keep the host and swap the PATH — pushing the
        # operator's code to `https://github.com/attacker/exfil.git`. Pin the IDENTITY too:
        # a network origin must name the same `owner/repo` as the validated `target_url`.
        # Compared transport-agnostically (see `_remote_slug`) because setup stores the ssh
        # form whenever `gh` prefers it, and refusing that would degrade every ssh install
        # to queue-only. A LOCAL path has no slug and stays allowed — it cannot exfiltrate.
        # Fail closed: when the two disagree, or `target_url` is missing/invalid so there is
        # nothing to pin against, there is no push target. Raised by the GPT review.
        direct_slug = _remote_slug(direct)
        if direct_slug:
            pinned = str((config or {}).get("target_url") or "").strip()
            spec, err = validate_target_url(pinned) if pinned else (None, "no target_url")
            if err or spec is None:
                logger.warning(
                    "stored origin_url names a remote repo but target_url does not validate, "
                    "so its identity cannot be pinned — no push target: %s",
                    err,
                )
                return ""
            if direct_slug != spec.display.lower():
                logger.warning(
                    "stored origin_url points at a different repository than the configured "
                    "target — no push target"
                )
                return ""
        return direct
    legacy = str((config or {}).get("target_url") or "").strip()
    if not legacy:
        return ""
    spec, err = validate_target_url(legacy)
    if err or spec is None:
        logger.warning("stored target_url did not validate — no push target: %s", err)
        return ""
    return spec.clone_url


def _disable_push(repo: Path) -> None:
    """Neutralize BOTH origin URLs so the clone cannot reach the remote at all.

    Disabling only the PUSH url is not enough. ``git push --push`` is honored only when
    the caller pushes *by remote name*; ``git push "$(git remote get-url origin)" HEAD``
    ignores the push url entirely and writes to the fetch url. The loop's agent runs with
    auto-approved Bash inside this clone, so a repository instruction could do exactly
    that. Verified against a local bare repo: pushing by name is refused, pushing to the
    fetch url lands a new branch upstream. Raised by review of this branch.

    The trusted publishers (the PR-draft recipe, the driver's F10 direct push, the
    operator's one-click commit) do NOT read the url out of this clone any more — it is
    carried in config as ``origin_url`` and handed to them explicitly, which is what keeps
    "one generated ref" a property of the code path rather than of the clone's config.

    Idempotent and best-effort across git versions: the caller re-verifies via
    :func:`_ok` / ``assert_push_disabled`` and fails closed if either url survives.
    """
    env = _git_env()
    for key in ("remote.origin.pushurl", "remote.origin.url"):
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                *_GIT_SAFE_CONFIG,
                "config",
                "--local",
                "--no-includes",
                "--unset-all",
                key,
            ],
            capture_output=True,
            timeout=30,
            shell=False,
            env=env,
            **UTF8_TEXT,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                *_GIT_SAFE_CONFIG,
                "config",
                "--local",
                "--no-includes",
                "--add",
                key,
                DISABLED_NO_PUSH,
            ],
            capture_output=True,
            timeout=30,
            shell=False,
            env=env,
            **UTF8_TEXT,
        )


def checkout_branch(clone: Path, branch: str, *, timeout_s: int = 120) -> tuple[bool, str]:
    """Put the clone's working tree on ``branch`` before a run reads its HEAD.

    A fresh clone sits on the repo's DEFAULT branch (usually ``main``). The run,
    however, targets ``config.branch`` — and when they differ the clone holds the
    wrong tree: dogfooding the app targets ``feat/auto-improvement-app`` but the
    clone was on ``main``, which does not even contain the app subtree, so the
    edit-allowlist focus matched zero files and discovery read code it could never
    fix. This fetches the branch and checks it out so ``head_sha()`` is the branch
    the user actually chose.

    ``branch`` may be given as ``origin/x`` or bare ``x`` (the config stores the
    former); both resolve to the same local branch tracking ``origin/x``.

    Fail-soft: if the fetch fails (offline) but a local ref already exists, check
    that out rather than aborting the run; only a branch we can locate NOWHERE is a
    hard error. Push stays disabled throughout — this never contacts the push URL.
    """
    clone = Path(clone)
    bare = branch.split("/", 1)[1] if branch.startswith("origin/") else branch
    if not bare or not is_valid_branch_name(bare):
        return False, f"invalid branch name: {branch!r}"
    try:
        if not _repository_is_safe(clone):
            return False, "clone Git metadata failed safety verification"
        disabled = _push_disabled(clone)
    except IsolationProbeError as exc:
        return False, str(exc)
    if not disabled:
        return False, "clone is not push-disabled"

    def _run(*args: str, tmo: int = timeout_s) -> subprocess.CompletedProcess:
        # Harden every host-side git over this clone: `checkout -B` below runs `post-checkout`
        # hooks and `git` consults `core.fsmonitor`, and this clone may already hold a tree a
        # prior agent pass edited — a repo-planted hook/fsmonitor program would execute
        # host-side. The two `-c` flags alone do NOT stop an attribute-bound
        # `filter.<n>.smudge`/`diff.<n>.textconv` (only `.git/info/attributes` does, and
        # `checkout` runs the smudge filter), so this must ALSO fail-closed-pin the attributes
        # via `require_pinned` — exactly like the other host-side helpers, through the ONE
        # shared config. Was re-declaring the `-c` pair inline, which both missed the
        # attribute vector and re-introduced the per-call-site drift the shared module removed.
        # Raised by the Opus 5 review.
        require_pinned(clone)
        return subprocess.run(
            [
                "git",
                "-C",
                str(clone),
                f"--work-tree={clone}",
                *_GIT_SAFE_CONFIG,
                *args,
            ],
            capture_output=True,
            timeout=tmo,
            shell=False,
            env=_git_env(),
            **UTF8_TEXT,
        )

    # Already there? Nothing to do — avoids a needless network fetch every run.
    cur = _run("rev-parse", "--abbrev-ref", "HEAD", tmo=30)
    if (cur.stdout or "").strip() == bare:
        return True, f"already on {bare}"

    fetched = _run("fetch", "--quiet", "origin", bare)
    if fetched.returncode == 0:
        co = _run("checkout", "-B", bare, f"origin/{bare}")
        if co.returncode == 0:
            return True, f"checked out {bare} @ origin/{bare}"
        err = (co.stderr or "").strip().splitlines()[-1:] or [""]
        return False, f"could not check out {bare}: {redact_via_context(err[0])[:160]}"
    # The fetch failed. That is the NORMAL case here, not an edge case: this clone's
    # origin is neutralized to DISABLED_NO_PUSH (both urls — see `_disable_push`), so
    # `git fetch origin <branch>` always exits 128. Measured against a local bare repo.
    #
    # Try the REMOTE-TRACKING ref before the local one. A fresh clone has
    # `origin/<branch>` for every branch on the remote but a LOCAL branch only for the
    # default one, so checking only for a local ref meant any non-default branch fell
    # through to "could not fetch" — and the caller's non-scoped path logs a warning and
    # starts anyway, which means the run discovers, edits and measures the DEFAULT branch
    # while the operator believes it is working on the one they configured. No network is
    # needed to fix it: the ref is already in the clone.
    #
    # Raised by the GPT review of this branch; same root cause as the one-click-commit
    # fetch bug (`commit.py`) — code inside a deliberately push-disabled clone cannot
    # reach the remote for READS either.
    remote_ref = f"origin/{bare}"
    if _run("rev-parse", "--verify", "--quiet", remote_ref, tmo=30).returncode == 0:
        co = _run("checkout", "-B", bare, remote_ref)
        if co.returncode == 0:
            return True, f"checked out {bare} @ {remote_ref} (no fetch — origin is disabled)"

    # Then a local branch, so a previously-fetched branch still runs offline.
    local = _run("rev-parse", "--verify", "--quiet", bare, tmo=30)
    if local.returncode == 0:
        co = _run("checkout", bare)
        if co.returncode == 0:
            return True, f"checked out local {bare} (fetch failed — offline?)"
    err = (fetched.stderr or "").strip().splitlines()[-1:] or [""]
    return False, f"could not fetch {bare}: {redact_via_context(err[0])[:160]}"


def _ok(spec: CloneSpec, dest: Path, *, reused: bool) -> dict:
    """Report success only after every origin URL is exactly the sentinel."""
    push_disabled = _push_disabled(dest)
    return {
        "ok": True,
        "display": spec.display,
        "clone": str(dest),
        "push_disabled": push_disabled,
        "reused": reused,
        # The real remote, for the trusted publishers only. Kept in config rather than in
        # the clone so agent-run Bash inside the clone cannot discover it from git.
        "origin_url": spec.clone_url,
    }

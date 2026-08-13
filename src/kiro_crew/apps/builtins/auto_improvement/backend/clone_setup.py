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
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
            proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
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
    """Validate ``url`` and clone it into ``scratch_root`` with push disabled.

    Returns ``(result_dict, "")`` on success or ``({}, reason)`` on a user-input
    problem. Idempotent: an existing clone of the same origin is reused after
    re-asserting push-disabled. Never follows a symlinked destination — a
    symlinked scratch dir must not let ``_disable_push`` rewrite a foreign repo.
    """
    spec, err = validate_target_url(url)
    if not spec:
        return {}, err

    scratch_root.mkdir(parents=True, exist_ok=True)
    dest = scratch_root / spec.dir_name

    if os.path.islink(dest):
        return {}, f"Destination is a symlink (refused for safety): {dest}"

    git_dir = dest / ".git"
    if not os.path.islink(git_dir) and git_dir.is_dir():
        actual_origin = subprocess.run(
            ["git", "-C", str(dest), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        ).stdout.strip()
        if actual_origin and actual_origin != spec.clone_url:
            return {}, (
                f"Existing clone at {dest} has origin {actual_origin!r}, which does not "
                f"match the requested {spec.clone_url!r} — refusing to reuse it."
            )
        _disable_push(dest)
        return _ok(spec, dest, reused=True), ""

    if dest.exists():
        return {}, f"Destination already exists and is not a git repo: {dest}"

    # argv list, shell=False, clone_url rebuilt from validated components.
    try:
        proc = subprocess.run(
            ["git", "clone", "--origin", "origin", spec.clone_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {}, f"git clone timed out after {timeout_s}s."
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {}, f"git clone failed: {tail[0][:200]}"

    _disable_push(dest)
    return _ok(spec, dest, reused=False), ""


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
    if not (clone / ".git").is_dir() and not (clone / ".git").is_file():
        return [], f"Not a git clone: {clone}"
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
        text=True,
        timeout=timeout_s,
        shell=False,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return [], f"could not list branches: {tail[0][:160]}"
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
        text=True,
        timeout=timeout_s,
        shell=False,
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
    for extra in (["--push"], []):
        subprocess.run(
            ["git", "-C", str(repo), "remote", "set-url", *extra, "origin", DISABLED_NO_PUSH],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
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
            ["git", "-C", str(clone), *_GIT_SAFE_CONFIG, *args],
            capture_output=True,
            text=True,
            timeout=tmo,
            shell=False,
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
        return False, f"could not check out {bare}: {err[0][:160]}"
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
    return False, f"could not fetch {bare}: {err[0][:160]}"


def _ok(spec: CloneSpec, dest: Path, *, reused: bool) -> dict:
    """Report success only after confirming push is actually disabled (fail closed)."""
    push = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "--push", "origin"],
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    fetch = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )

    def _neutral(proc: subprocess.CompletedProcess) -> bool:
        url = (proc.stdout or "").strip()
        return proc.returncode == 0 and (
            (not url) or ("DISABLED" in url.upper()) or ("NO_PUSH" in url.upper())
        )

    # BOTH urls must be neutral. A live fetch url is a live push target
    # (`git push "$(git remote get-url origin)"`), so checking only the push url
    # reported "disabled" for a clone that could still write to the remote.
    push_disabled = _neutral(push) and _neutral(fetch)
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

#!/usr/bin/env python3
"""preflight.py - deterministic Phase-0 gate for the prepare-pr skill.

Reports repo / current-branch / base-branch / gh-auth / dirty / divergence /
existing-PR and gates on blockers so the agent never commits on the base
branch or acts unauthenticated.

Portable: stdlib only; shells out to git/gh via argument lists (no shell
pipelines), so it runs on macOS, Linux, and Windows wherever KiroCrew's
python3 plus git/gh are available.

Usage:  python3 preflight.py
Exit:   0 READY | 30 BLOCKER (see printed reason) | 2 environment error
"""

import json
import os
import re
import shutil
import subprocess
import sys

from push_guard import DEFAULT_MAX_AHEAD, _classify_fetch_error

_WORKTREE_ROOT_UNRESOLVED = object()
_WORKTREE_ROOT = _WORKTREE_ROOT_UNRESOLVED

# Bound on each network probe (gh lookups, git push --dry-run) and the
# bootstrap worktree-root lookup. A Phase-0 probe must terminate: an
# interactive transport or planted executable must never hang preflight.
_PROBE_TIMEOUT_SECS = 60


def _resolve_worktree_root():
    """Resolve and cache the Git working-tree root without re-entering run()."""
    global _WORKTREE_ROOT
    if _WORKTREE_ROOT is not _WORKTREE_ROOT_UNRESOLVED:
        return _WORKTREE_ROOT
    git_exe = shutil.which("git")
    if git_exe is None:
        _WORKTREE_ROOT = ""
        return _WORKTREE_ROOT
    git_exe_abs = os.path.abspath(git_exe)
    if _path_is_within(git_exe_abs, os.getcwd()):
        _WORKTREE_ROOT = ""
        return _WORKTREE_ROOT
    try:
        proc = subprocess.run(
            [git_exe_abs, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PROBE_TIMEOUT_SECS,
        )
    except (subprocess.TimeoutExpired, OSError):
        _WORKTREE_ROOT = ""
        return _WORKTREE_ROOT
    if proc.returncode != 0 or not proc.stdout.strip():
        _WORKTREE_ROOT = ""
        return _WORKTREE_ROOT
    _WORKTREE_ROOT = os.path.abspath(proc.stdout.strip())
    return _WORKTREE_ROOT


def _path_is_within(path, root):
    """Return whether path is root or one of its descendants."""
    path_norm = os.path.normcase(os.path.abspath(path))
    root_norm = os.path.normcase(os.path.abspath(root))
    return path_norm == root_norm or path_norm.startswith(root_norm.rstrip(os.sep) + os.sep)


def run(args, extra_env=None, timeout=None):
    """Run a command; return (returncode, stdout, stderr) as stripped text.

    Never raises - a missing executable is reported as rc 127. ``extra_env``
    overlays the inherited environment for this one call.  ``timeout`` (in
    seconds) bounds the subprocess: on expiry the child is killed and the
    call reports rc 124 (the shell ``timeout`` convention) with a hardcoded
    message, so a probe that would otherwise block forever (e.g. an
    interactive transport waiting on a tty) always terminates.

    argv[0] is resolved with ``shutil.which`` first: Windows CreateProcess
    appends only ``.exe`` when searching PATH, so a ``gh.cmd`` / ``git.cmd``
    launcher (scoop, npm, corporate shims) is invisible to a bare
    ``subprocess.run`` even though every shell finds it. ``which`` honors
    PATHEXT and gives all platforms the same lookup a shell performs.

    Launching a ``.cmd``/``.bat`` file makes cmd.exe the real parser of the
    argument list, and cmd.exe expands metacharacters inside arguments
    (CVE-2024-24576, "BatBadBut").  Arguments here can carry values read from
    the cloned repo's own config (remote URLs, branch names), so when the
    resolved target is a batch launcher, any argument containing a cmd.exe
    metacharacter fails closed as rc 126 instead of being passed through.

    On Windows the PATH search also visits the CURRENT DIRECTORY, and this
    script runs inside the cloned repo - so a checkout carrying its own
    ``gh.exe``/``gh.cmd`` would otherwise win the lookup and run attacker
    code.  Any resolution landing inside the working tree fails closed as
    rc 126, whatever its extension.
    """
    try:
        env = None
        if extra_env:
            env = dict(os.environ)
            env.update(extra_env)
        exe = shutil.which(args[0])
        if exe is None:
            return 127, "", "{}: not found on PATH".format(args[0])
        exe_abs = os.path.abspath(exe)
        fence_root = _resolve_worktree_root() or os.getcwd()
        repo_local = _path_is_within(exe_abs, fence_root)
        if repo_local:
            return (
                126,
                "",
                ("{}: refusing executable resolved inside the working " "tree".format(args[0])),
            )
        rest = list(args[1:])
        if exe_abs.lower().endswith((".cmd", ".bat")):
            bad = [a for a in rest if any(ch in a for ch in '^&|<>%!"\r\n')]
            if bad:
                return (
                    126,
                    "",
                    (
                        "{}: refusing batch-file launch with cmd.exe "
                        "metacharacters in arguments".format(args[0])
                    ),
                )
        p = subprocess.run(
            [exe] + rest,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "{}: timed out after {}s".format(args[0], timeout)
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


# Permissions that mean the run credential can push to the target repo.  gh's
# viewerPermission is upper-case (ADMIN/MAINTAIN/WRITE/TRIAGE/READ/NONE); the
# REST permissions object uses lower-case (admin/maintain/write/...).  Compared
# case-insensitively so either source lands here.
_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

# Write-permission verdict states.  These mirror author_write_verdict's
# writer/other/unknown discipline (see _review_contract.py): a DEFINITIVE
# non-writer must block, but a TRANSIENT lookup failure must not, so a real
# writer is never stranded on a rate-limit blip.
_WRITE_WRITER = "writer"  # can push -> not a blocker
_WRITE_DENIED = "denied"  # definitively cannot push -> fail-fast BLOCKER
_WRITE_UNKNOWN = "unknown"  # transient/indeterminate -> WARNING, proceed


def resolve_target_repo():
    """Return the target repo as 'owner/name', or '' when it cannot be resolved.

    Uses the same call pr_status.py's consumers rely on
    (`gh repo view --json nameWithOwner`).  Never surfaces raw stderr.
    """
    rc, out, _ = run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        timeout=_PROBE_TIMEOUT_SECS,
    )
    if rc == 0 and out:
        return out.strip()
    return ""


def viewer_write_verdict(repo):
    """Classify the run credential's push access to ``repo`` via gh.

    Returns (verdict, detail) where verdict is one of writer/denied/unknown and
    detail is a hardcoded-safe label (never raw gh stderr) describing why.

    `gh repo view <repo> --json viewerPermission` answers ADMIN/MAINTAIN/WRITE
    (write-capable) or TRIAGE/READ/NONE (definitively not).  Any lookup failure
    or missing field reads as unknown - no stderr inspection; the push
    transport probe is the authority that then decides (see
    push_transport_verdict), so a gh blip never has to be classified here.
    """
    if not repo:
        return _WRITE_UNKNOWN, "target repo unresolved"

    rc, out, _ = run(
        ["gh", "repo", "view", repo, "--json", "viewerPermission"],
        timeout=_PROBE_TIMEOUT_SECS,
    )
    if rc == 124:
        return _WRITE_UNKNOWN, "viewerPermission lookup timed out"
    if rc == 0 and out:
        try:
            perm = (json.loads(out).get("viewerPermission") or "").strip()
        except (ValueError, AttributeError):
            perm = ""
        if perm:
            if perm.lower() in _WRITE_PERMISSIONS:
                return _WRITE_WRITER, "viewerPermission={}".format(perm)
            return _WRITE_DENIED, "viewerPermission={}".format(perm)
    return _WRITE_UNKNOWN, "viewerPermission unavailable"


# Shapes that prove the remote EVALUATED the ref update - reaching that
# evaluation requires an accepted credential, so a rejected non-fast-forward
# dry-run is transport-confirmed write access, not a failure of it.
_PUSH_REJECTED_RE = re.compile(r"non-fast-forward|fetch first|\[rejected\]", re.IGNORECASE)

# Definitive denials whose missing piece is a NON-INTERACTIVE PUSH CREDENTIAL
# rather than repo permission.  The remedy is credential setup, not a fork -
# a fork push rides the same absent/locked credential.
_PUSH_NO_CREDENTIAL_RE = re.compile(
    r"could not read (?:Username|Password)|permission denied \(publickey", re.IGNORECASE
)

# Definitive permission/authentication denials from the transport.
_PUSH_DENIED_RE = re.compile(
    r"permission denied|denied to |not authorized|authentication failed"
    r"|repository not found|HTTP 40[13]\b|returned error: 40[13]\b",
    re.IGNORECASE,
)


def _classify_push_probe_error(stderr):
    """Classify a failed ``git push --dry-run`` into (verdict, detail).

    NEVER returns raw stderr - git transport errors can carry credential
    tokens or remote URLs with embedded auth.  Only hardcoded labels are
    surfaced.

    A non-fast-forward rejection is WRITER: the remote evaluated the ref
    update, which it only does for an accepted credential.  A missing
    non-interactive credential (``could not read Username`` under
    ``GIT_TERMINAL_PROMPT=0``, ``Permission denied (publickey)`` under batch
    mode) is a definitive denial with a credential-setup remedy.  Other
    permission/authentication shapes are definitive denials with the fork
    remedy.  Anything else (network, DNS, odd transport) is transient and
    reads as unknown.
    """
    text = stderr or ""
    if _PUSH_REJECTED_RE.search(text):
        return _WRITE_WRITER, "push dry-run rejected non-fast-forward (write access confirmed)"
    if _PUSH_NO_CREDENTIAL_RE.search(text):
        return _WRITE_DENIED, "no non-interactive git push credential"
    if _PUSH_DENIED_RE.search(text):
        return _WRITE_DENIED, "origin push transport denied the dry-run"
    return _WRITE_UNKNOWN, "push dry-run inconclusive"


def _push_probe_env():
    """Env overlay that keeps the dry-run non-interactive WITHOUT changing
    which SSH identity/config the real push would use.

    ``GIT_TERMINAL_PROMPT=0`` stops git's own credential prompts, and
    ``LC_ALL=C`` pins the client-side message locale so a definitive denial
    shape (e.g. ``could not read Username``) is never degraded to a warning
    by a translated message on a non-English host.  For SSH,
    batch mode is APPENDED to the ssh command the push would actually run
    (inherited ``GIT_SSH_COMMAND``, else ``core.sshCommand``, else plain
    ``ssh``), so a per-repo identity or custom config is preserved.  When the
    legacy ``GIT_SSH`` program variable is in effect it takes no options, so
    it is left untouched rather than overridden; the probe timeout
    (``_PROBE_TIMEOUT_SECS``, applied in ``push_transport_verdict``) bounds
    the hang risk an interactive legacy program would otherwise pose.
    """
    env = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    ssh_command = os.environ.get("GIT_SSH_COMMAND") or run(["git", "config", "core.sshCommand"])[1]
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command + " -oBatchMode=yes"
    elif not os.environ.get("GIT_SSH"):
        env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    return env


def push_transport_verdict(branch):
    """Ask the origin push transport itself whether this run can push.

    The gh probe judges the repo gh RESOLVES, with the gh credential - but
    ``git push`` rides its own transport (an SSH identity on origin, a
    credential helper), and in a fork-clone layout gh resolves the read-only
    parent while origin is the writable fork.  ``git push --dry-run``
    exercises the real transport end to end without transferring objects or
    updating any ref, so its answer outranks any proxy.

    The probe observes ONLY the transport: ``--no-verify`` skips pre-push
    hooks (hook output is not evidence about push permission), and the env
    from ``_push_probe_env`` keeps it non-interactive while preserving the
    SSH identity/config the real push would use.

    Returns (verdict, detail): rc 0 confirms the transport can push; expiry of
    the probe timeout reads as unknown (an interactive transport that would
    wait forever must never hang Phase 0); any other failed dry-run is
    classified by ``_classify_push_probe_error``.  detail is a hardcoded-safe
    label - raw git stderr is never surfaced.
    """
    rc, _, stderr = run(
        ["git", "push", "--dry-run", "--no-verify", "--quiet", "origin", branch],
        extra_env=_push_probe_env(),
        timeout=_PROBE_TIMEOUT_SECS,
    )
    if rc == 124:
        return _WRITE_UNKNOWN, "push dry-run timed out"
    if rc == 0:
        return _WRITE_WRITER, "push dry-run ok"
    return _classify_push_probe_error(stderr)


def main():
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository (or git not found).")
        return 2

    root = run(["git", "rev-parse", "--show-toplevel"])[1]
    cur = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])[1]

    # gh auth + existing PR (GitHub path).
    gh_ok = run(["gh", "auth", "status"])[0] == 0
    pr_num = pr_url = pr_base = ""
    # Write-permission preflight state (only meaningful when gh is authed).
    target_repo = ""
    write_verdict = _WRITE_UNKNOWN
    write_detail = "not checked (gh not authenticated)"
    fork_hint = ""
    if gh_ok:
        rc, out, _ = run(["gh", "pr", "view", "--json", "number,url,baseRefName"])
        if rc == 0 and out:
            try:
                d = json.loads(out)
                pr_num = str(d.get("number") or "")
                pr_url = d.get("url") or ""
                pr_base = d.get("baseRefName") or ""
            except ValueError:
                pass

        # Fail-fast write-permission gate: verify the run credential can push
        # to the target repo BEFORE any work is done, so a run that cannot
        # push is not discovered only at push time.  A definitive non-writer
        # blocks and is routed to the fork path; a transient lookup failure
        # only WARNs so a real writer is not stranded on a blip.
        target_repo = resolve_target_repo()
        write_verdict, write_detail = viewer_write_verdict(target_repo)
        if cur and cur != "HEAD":
            # The transport probe runs unconditionally: git push rides its
            # own credential (SSH identity, credential helper), and in a
            # fork-clone layout gh resolves the read-only parent while origin
            # is the writable fork.  Its accept or definitive refusal
            # outranks the gh proxy; only when it is inconclusive does the
            # gh verdict stand - and a gh 'writer' with an unproven accept
            # softens to unknown (WARN), because the accept is what the run
            # is actually counting on.
            transport_verdict, transport_detail = push_transport_verdict(cur)
            if transport_verdict == _WRITE_WRITER:
                write_detail = (
                    write_detail + ", " + transport_detail
                    if write_verdict == _WRITE_WRITER
                    else "{} ({} on gh probe)".format(transport_detail, write_detail)
                )
                write_verdict = _WRITE_WRITER
            elif transport_verdict == _WRITE_DENIED:
                write_detail = "{} ({} on gh probe)".format(transport_detail, write_detail)
                write_verdict = _WRITE_DENIED
            elif write_verdict == _WRITE_WRITER:
                write_verdict = _WRITE_UNKNOWN
                write_detail = "gh reports writer, but " + transport_detail
            else:
                write_detail = write_detail + ", " + transport_detail
        if write_verdict == _WRITE_DENIED:
            if "no non-interactive git push credential" in write_detail:
                # The missing piece is a credential, not repo permission - a
                # fork push would ride the same absent/locked credential.
                fork_hint = (
                    "at a terminal, run ssh-add to load the passphrase-locked SSH key into "
                    "the ssh-agent, or run gh auth setup-git for HTTPS, then re-run this preflight"
                )
            else:
                fork_hint = (
                    "if this could be a transient auth blip (e.g. a credential-helper "
                    "hiccup), re-run this preflight once before forking; otherwise "
                    "create or reuse a fork (gh repo fork {} --clone=false, which detects an "
                    "existing fork) and push there".format(target_repo or "<owner>/<repo>")
                )

    # Base branch: prefer an existing PR's base, else origin/HEAD, else "main".
    base = pr_base
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[1]
        if sym.startswith("origin/"):
            base = sym[len("origin/") :]
        else:
            base = sym
    if not base:
        base = "main"

    on_protected = cur == base
    dirty = bool(run(["git", "status", "--porcelain"])[1])

    # Divergence vs base (non-destructive fetch).  The fetch MUST succeed —
    # without a fresh origin/<base> ref every subsequent merge-base/rebase
    # operates on a potentially stale local copy (root cause of the clobber
    # incident where a force-push carried 114 duplicate commits).
    # Uses an explicit refspec so the remote-tracking ref is always updated
    # regardless of the clone's configured remote.origin.fetch (single-branch
    # clones, narrow CI checkouts).
    behind = ahead = "?"
    refspec = "+refs/heads/{}:refs/remotes/origin/{}".format(base, base)
    fetch_rc, _, fetch_err = run(["git", "fetch", "--quiet", "origin", refspec])
    fetch_ok = fetch_rc == 0
    if fetch_ok:
        rc, out, _ = run(
            ["git", "rev-list", "--left-right", "--count", "origin/{}...HEAD".format(base)]
        )
        if rc == 0 and len(out.split()) == 2:
            behind, ahead = out.split()

    print("repo:            " + root)
    print("current branch:  " + cur)
    print("base branch:     " + base + ("  (from PR)" if pr_base else ""))
    print("on protected:    " + ("yes" if on_protected else "no"))
    print("working tree:    " + ("dirty" if dirty else "clean"))
    print("fetch origin:    " + ("ok" if fetch_ok else "FAILED"))
    print("vs origin/{}:  behind={} ahead={}".format(base, behind, ahead))
    print("gh authed:       " + ("yes" if gh_ok else "no"))
    print("existing PR:     " + (pr_num or "none") + (("  (" + pr_url + ")") if pr_url else ""))
    if gh_ok:
        print("target repo:     " + (target_repo or "unresolved"))
        if write_verdict == _WRITE_WRITER:
            write_label = "yes"
        elif write_verdict == _WRITE_DENIED:
            write_label = "NO"
        else:
            write_label = "unknown"
        print("write access:    " + write_label + "  (" + write_detail + ")")
        if fork_hint:
            print("fork path:       " + fork_hint)

    blocked = False
    if cur == "HEAD":
        print(
            "BLOCKER: detached HEAD (no branch checked out) - switch to a "
            "feature branch first: git switch -c <type>/<slug>"
        )
        blocked = True
    elif on_protected:
        print(
            "BLOCKER: on the integration branch '{}' - create a feature branch "
            "first: git switch -c <type>/<slug>".format(cur)
        )
        blocked = True
    if not gh_ok:
        print("BLOCKER: gh not authenticated - run: gh auth login")
        blocked = True
    if gh_ok and write_verdict == _WRITE_DENIED:
        # The run credential definitively cannot push to the target repo.
        # Fail fast at Phase 0 rather than stranding a completed change on a
        # local branch at push time.  Surface the remedy - the fork path, or
        # credential setup when the missing piece is the credential itself -
        # so the run can route there from the start, or be scoped read-only.
        print(
            "BLOCKER: no write access to {} ({}) - this run cannot push to the "
            "target repo. Do NOT do write-producing work that will be stranded "
            "at push time. Remedy: {}. Or scope this run "
            "to read-only analysis (no branch/commit/push).".format(
                target_repo or "the target repo",
                write_detail,
                fork_hint or "route through a fork (gh repo fork ...)",
            )
        )
        blocked = True
    elif gh_ok and write_verdict == _WRITE_UNKNOWN:
        # Transient/indeterminate permission lookup (5xx, rate limit, network,
        # unresolved repo).  Never a hard block on this basis alone - that
        # would strand a legitimate writer on a blip - so warn and proceed,
        # consistent with author_write_verdict's 'unknown' never being acted on.
        print(
            "WARNING: could not confirm write access to {} ({}). Proceeding, "
            "but if a later push fails with a permission error, re-run this "
            "preflight and route through a fork.".format(
                target_repo or "the target repo", write_detail
            )
        )
    if not fetch_ok:
        print(
            "BLOCKER: git fetch origin {} failed — cannot verify branch "
            "freshness against the remote base. Rebase/push on a stale ref "
            "risks clobbering upstream work. Fix network/auth and retry.".format(base)
        )
        if fetch_err:
            # Derive a safe diagnostic from stderr — never pass raw text
            # through (free-text can carry bare tokens from remote helpers
            # or credential-helper error messages that no URL-shape scrubber
            # can redact; round-13 lesson).
            print("  error class: " + _classify_fetch_error(fetch_err))
        blocked = True
    # Stale-base guard: if the branch is implausibly far ahead of origin/<base>
    # (more than DEFAULT_MAX_AHEAD commits for a single-commit PR workflow),
    # warn loudly.  This catches worktrees that were branched from a local
    # trunk carrying unshipped integration commits (root cause of the
    # clobber).  The threshold is generous — a normal prepare-pr
    # squashes to 1 commit; DEFAULT_MAX_AHEAD allows for multi-commit profiles
    # or a small rebase stack.
    if ahead != "?" and int(ahead) > DEFAULT_MAX_AHEAD:
        print(
            "WARNING: branch is {} commits ahead of origin/{} — this is "
            "unusually high for a single-commit PR. If this branch was created "
            "from a local integration trunk, those extra commits will be "
            "replayed on rebase and force-pushed to the remote, potentially "
            "clobbering upstream work. Verify the branch history before "
            "proceeding.".format(ahead, base)
        )
    if blocked:
        return 30

    print("STATUS: READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())

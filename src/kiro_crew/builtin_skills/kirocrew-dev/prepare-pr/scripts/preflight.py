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
import re
import subprocess
import sys

from push_guard import DEFAULT_MAX_AHEAD, _classify_fetch_error


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text.

    Never raises - a missing executable is reported as rc 127.
    """
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
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


def _classify_permission_error(stderr):
    """Classify a gh permission-lookup failure into writer/denied/unknown.

    NEVER returns raw stderr - gh error text can carry credential tokens
    (round-13 lesson, same discipline as push_guard._classify_fetch_error).
    Only the hardcoded verdict label is surfaced.

    Rate-limit / abuse-detection 403 is transient exactly like a 429 (the same
    carve-out author_write_verdict makes), so it reads as unknown.  A
    non-rate-limit HTTP 403/404 is a DEFINITIVE "this credential cannot write
    here".  Anything else (5xx, network, empty/unparseable body) is transient.
    """
    text = stderr or ""
    if re.search(r"rate limit|abuse detection", text, re.IGNORECASE):
        return _WRITE_UNKNOWN
    if re.search(r"HTTP (?:404|403)\b", text):
        return _WRITE_DENIED
    return _WRITE_UNKNOWN


def resolve_target_repo():
    """Return the target repo as 'owner/name', or '' when it cannot be resolved.

    Uses the same call pr_status.py's consumers rely on
    (`gh repo view --json nameWithOwner`).  Never surfaces raw stderr.
    """
    rc, out, _ = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
    if rc == 0 and out:
        return out.strip()
    return ""


def viewer_write_verdict(repo):
    """Classify the run credential's push access to ``repo``.

    Returns (verdict, detail) where verdict is one of writer/denied/unknown and
    detail is a hardcoded-safe label (never raw gh stderr) describing why.

    Prefers `gh repo view <repo> --json viewerPermission` (ADMIN/MAINTAIN/WRITE
    are write-capable).  Falls back to `gh api repos/<repo> --jq .permissions.push`
    (true == write-capable) when viewerPermission is unavailable.  A definitive
    non-writer permission or a definitive HTTP 403/404 blocks; a transient
    failure is reported as unknown so a real writer is not stranded on a blip.
    """
    if not repo:
        return _WRITE_UNKNOWN, "target repo unresolved"

    rc, out, stderr = run(["gh", "repo", "view", repo, "--json", "viewerPermission"])
    if rc == 0 and out:
        try:
            perm = (json.loads(out).get("viewerPermission") or "").strip()
        except (ValueError, AttributeError):
            perm = ""
        if perm:
            if perm.lower() in _WRITE_PERMISSIONS:
                return _WRITE_WRITER, "viewerPermission={}".format(perm)
            return _WRITE_DENIED, "viewerPermission={}".format(perm)
        # rc 0 but no usable permission field -> fall through to the REST probe.
    else:
        verdict = _classify_permission_error(stderr)
        if verdict == _WRITE_DENIED:
            return _WRITE_DENIED, "repo view returned HTTP 403/404"

    # Fallback: REST permissions.push (true == write-capable).
    rc, out, stderr = run(["gh", "api", "repos/{}".format(repo), "--jq", ".permissions.push"])
    if rc == 0:
        answer = out.strip().lower()
        if answer == "true":
            return _WRITE_WRITER, "permissions.push=true"
        if answer == "false":
            return _WRITE_DENIED, "permissions.push=false"
        return _WRITE_UNKNOWN, "permissions.push indeterminate"
    return _classify_permission_error(stderr), "permission lookup failed"


def fork_path_hint(repo):
    """Return an actionable fork-path label for a repo the run cannot push to.

    Reports whether a fork under the current viewer already exists so the agent
    can route there from the start instead of discovering the block at push
    time.  Never surfaces raw gh stderr; only the hardcoded labels below.
    """
    viewer = ""
    rc, out, _ = run(["gh", "api", "user", "--jq", ".login"])
    if rc == 0 and out.strip():
        viewer = out.strip()

    name = repo.split("/", 1)[1] if "/" in repo else repo
    if viewer and name:
        fork_slug = "{}/{}".format(viewer, name)
        rc, _, _ = run(["gh", "repo", "view", fork_slug, "--json", "nameWithOwner"])
        if rc == 0:
            return "existing fork {} - push there and open the PR cross-fork".format(fork_slug)
    return "no fork detected - create one (gh repo fork {} --clone=false) then push to it".format(
        repo
    )


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
        # push is not discovered only at push time (issue #9244).  A definitive
        # non-writer blocks and is routed to the fork path; a transient lookup
        # failure only WARNs so a real writer is not stranded on a blip.
        target_repo = resolve_target_repo()
        write_verdict, write_detail = viewer_write_verdict(target_repo)
        if write_verdict == _WRITE_DENIED:
            fork_hint = fork_path_hint(target_repo)

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
        # local branch at push time (issue #9244).  Surface the fork path so
        # the run can route there from the start, or be scoped to read-only.
        print(
            "BLOCKER: no write access to {} ({}) - this run cannot push to the "
            "target repo. Do NOT do write-producing work that will be stranded "
            "at push time. Route through a fork instead: {}. Or scope this run "
            "to read-only analysis (no branch/commit/push).".format(
                target_repo or "the target repo", write_detail, fork_hint or "gh repo fork ..."
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

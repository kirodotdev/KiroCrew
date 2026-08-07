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
import subprocess
import sys

from push_guard import DEFAULT_MAX_AHEAD, _classify_fetch_error


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text.

    Never raises - a missing executable is reported as rc 127.
    """
    try:
        p = subprocess.run(args, capture_output=True, text=True, errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def main():
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository (or git not found).")
        return 2

    root = run(["git", "rev-parse", "--show-toplevel"])[1]
    cur = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])[1]

    # gh auth + existing PR (GitHub path).
    gh_ok = run(["gh", "auth", "status"])[0] == 0
    pr_num = pr_url = pr_base = ""
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
    # operates on a potentially stale local copy (root cause of the 2026-07-31
    # clobber incident where a force-push carried 114 duplicate commits).
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
    # 2026-07-31 clobber).  The threshold is generous — a normal prepare-pr
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

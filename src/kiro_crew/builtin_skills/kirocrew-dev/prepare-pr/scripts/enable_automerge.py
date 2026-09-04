#!/usr/bin/env python3
"""enable_automerge.py - enable GitHub auto-merge for the prepare-pr skill.

A thin, idempotent wrapper around ``gh pr merge --auto``: it asks GitHub to
merge the PR automatically once the REPO'S OWN merge requirements (its required
reviews + required status checks, as configured in branch protection / rulesets)
are satisfied. GitHub, not this script, enforces those gates and performs the
merge. Defaults to squash to match KiroCrew's single-commit-per-PR invariant.

The prepare-pr skill invokes this only on an explicit ship/land request.

Portable: stdlib only; shells out to gh via argument lists (no shell
pipelines), so it runs on macOS, Linux, and Windows wherever KiroCrew's
python3 plus the GitHub CLI are available.

Usage:  python3 enable_automerge.py [pr-number] [method]
          pr-number  optional; auto-detected from the current branch if omitted
          method     squash (default) | merge | rebase
Exit:   0  auto-merge enabled (or already enabled)
       20  could not enable ('Allow auto-merge' disabled on the repo, no branch
           rule, method not permitted, missing permission, or PR closed/merged)
        2  environment / usage error (gh missing, not authed, no PR resolved)
"""

import json
import subprocess
import sys

VALID_METHODS = ("squash", "merge", "rebase")


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def main(argv):
    pr = ""
    method = "squash"
    for arg in argv:
        if arg in VALID_METHODS:
            method = arg
        elif arg.isdigit():
            pr = arg
        elif arg.startswith("#") and arg[1:].isdigit():
            pr = arg[1:]
        else:
            err(
                "ERROR: unrecognized argument '{}' (expected a PR number or one "
                "of {}).".format(arg, "|".join(VALID_METHODS))
            )
            return 2

    if run(["gh", "--version"])[0] != 0:
        err("ERROR: gh CLI not found. Install/authenticate the GitHub CLI.")
        return 2
    if run(["gh", "auth", "status"])[0] != 0:
        err("ERROR: gh not authenticated. Run: gh auth login")
        return 2

    if not pr:
        rc, out, _ = run(["gh", "pr", "view", "--json", "number", "-q", ".number"])
        pr = out if rc == 0 else ""
    if not pr:
        err("ERROR: no PR number given and none found for the current branch.")
        return 2

    # Idempotent: if auto-merge is already enabled, report and stop.
    rc, out, _ = run(["gh", "pr", "view", pr, "--json", "autoMergeRequest"])
    if rc == 0 and out:
        try:
            m = (json.loads(out).get("autoMergeRequest") or {}).get("mergeMethod")
        except ValueError:
            m = None
        if m:
            print(
                "[automerge] PR #{} already has auto-merge enabled "
                "(method={}).".format(pr, m.lower())
            )
            return 0

    rc, out, e = run(["gh", "pr", "merge", pr, "--auto", "--" + method])
    if rc == 0:
        print(
            "[automerge] enabled auto-merge (--{}) on PR #{} - GitHub will "
            "merge it once the repo's required reviews + checks are met.".format(method, pr)
        )
        return 0

    err("[automerge] could not enable auto-merge on PR #{}:".format(pr))
    for line in (out, e):
        if line:
            err("  " + line)
    err(
        "[automerge] common causes: 'Allow auto-merge' disabled on the repo, no "
        "branch rule to gate it, the method is not permitted, or the PR is "
        "closed/merged."
    )
    return 20


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

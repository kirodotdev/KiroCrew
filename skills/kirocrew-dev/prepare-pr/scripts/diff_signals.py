#!/usr/bin/env python3
"""diff_signals.py - change-signal inventory for the description-reconcile step.

Lists changed files by status vs the base branch and flags notable structural
signals so the PR body can be checked for completeness against what the diff
actually does. Stdlib only; portable across OSes.

Usage:  python3 diff_signals.py [base-branch]
Exit:   0 printed | 2 environment error
"""
import re
import subprocess
import sys

SIGNALS = [
    (r"(^|/)(package\.json|requirements.*\.txt|Cargo\.toml|go\.mod|pom\.xml|"
     r"build\.gradle|setup\.(py|cfg)|pyproject\.toml)",
     "dependency/manifest changed - call out added/removed deps"),
    (r"(^|/)(package-lock\.json|yarn\.lock|Cargo\.lock|poetry\.lock|go\.sum)",
     "lockfile changed"),
    (r"(migrations?/|/migrate)", "database/migration change"),
    (r"(^|/)\.github/workflows/", "CI workflow changed"),
    (r"(?m)^D\t", "files DELETED - call out removals"),
    (r"(?m)^R[0-9]*\t", "files RENAMED/moved"),
    (r"(Dockerfile|\.tf$|\.ya?ml$|\.toml$|\.ini$|(^|/)config)",
     "config/infra file changed"),
]


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def main(argv):
    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository (or git not found).")
        return 2

    base = argv[1] if len(argv) > 1 else ""
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short",
                   "refs/remotes/origin/HEAD"])[1].strip()
        base = sym[len("origin/"):] if sym.startswith("origin/") else ""
    if not base:
        base = "main"

    if run(["git", "rev-parse", "--verify", "--quiet", "origin/" + base])[0] != 0:
        err("ERROR: origin/{0} not found - run: git fetch origin {0}".format(base))
        return 2

    rng = "origin/{}...HEAD".format(base)
    print("=== Change vs origin/{} ===".format(base))
    stat = run(["git", "diff", "--stat", rng])[1].rstrip().splitlines()
    print(stat[-1] if stat else "(no changes)")
    print()
    print("=== Files (name-status) ===")
    ns = run(["git", "diff", "--name-status", rng])[1].rstrip()
    print(ns if ns else "(no changes vs base)")
    print()
    print("=== Signals (verify the PR body accounts for each) ===")
    any_flag = False
    for pat, msg in SIGNALS:
        if re.search(pat, ns, re.IGNORECASE | re.MULTILINE):
            print("! " + msg)
            any_flag = True
    if not any_flag:
        print("(no notable structural signals - still describe the behavior changes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

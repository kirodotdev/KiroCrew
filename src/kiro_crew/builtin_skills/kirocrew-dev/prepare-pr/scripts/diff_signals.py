#!/usr/bin/env python3
"""diff_signals.py - change-signal inventory for the description-reconcile step.

Lists changed files by status vs the base branch and flags notable structural
signals so the PR body can be checked for completeness against what the diff
actually does. Stdlib only; portable across OSes.

With ``--check-body`` it also reads the PR body the skill writes --
``<git-dir>/prepare-pr-body.md``, with ``<git-dir>`` from
``git rev-parse --absolute-git-dir`` -- and runs two checks on it:

* **Accounting (hard).** Every changed *area* -- the unit ``pr-scope.yml`` counts,
  a module directory for ``src/kiro_crew/`` and ``website/src/``, the top-level
  component elsewhere -- must be named somewhere in the body, by the area itself,
  by a changed file's path (or a ``dir/file`` tail of it), or by a changed file's
  bare name when that name is unique in the diff and not generic. A name counts
  only on a token boundary (``src/kiro_crew/chat`` is not named by
  ``src/kiro_crew/chat_runner.py``, ``test`` is not named by ``## Tests``). An area
  the body never names is a change a reviewer is not told about; that is exit 20.
  The body path is fixed, not an argument: the script reads exactly one file,
  inside git's own directory, so there is nothing to point at anything else.
  Unknown arguments are an error (exit 2).
* **Length (soft).** The prose of ``## What changed`` -- fenced blocks, table rows
  and image lines excluded -- is counted against ``SOFT_WORDS``, the length the
  contract's "three short paragraphs at most" come to. Over it prints a WARN and
  still exits 0: a wide cross-cutting change legitimately needs the words, so the
  limit is a nudge, never a gate.

Usage:  python3 diff_signals.py [base-branch] [--check-body]
Exit:   0 printed / body accounts for every area | 20 unaccounted area(s) | 2 environment error
"""

import argparse
import os
import re
import subprocess
import sys

SIGNALS = [
    (
        r"(^|/)(package\.json|requirements.*\.txt|Cargo\.toml|go\.mod|pom\.xml|"
        r"build\.gradle|setup\.(py|cfg)|pyproject\.toml)",
        "dependency/manifest changed - call out added/removed deps",
    ),
    (r"(^|/)(package-lock\.json|yarn\.lock|Cargo\.lock|poetry\.lock|go\.sum)", "lockfile changed"),
    (r"(migrations?/|/migrate)", "database/migration change"),
    (r"(^|/)\.github/workflows/", "CI workflow changed"),
    (r"(?m)^D\t", "files DELETED - call out removals"),
    (r"(?m)^R[0-9]*\t", "files RENAMED/moved"),
    (r"(Dockerfile|\.tf$|\.ya?ml$|\.toml$|\.ini$|(^|/)config)", "config/infra file changed"),
]

# Three short paragraphs of ~100 words. SKILL.md's PR description contract states
# the same number next to its paragraph rule; a test keeps the two equal.
SOFT_WORDS = 300

# git prints every path with forward slashes, on every OS. These helpers split
# and join git's own output, never filesystem paths, so the separator is git's.
GIT_SEP = "/"

# The one body file --check-body reads, relative to `git rev-parse --absolute-git-dir`.
BODY_FILENAME = "prepare-pr-body.md"

# Bare file names that appear in many places at once; naming one of these does
# not tell the reader WHICH one changed, so it never satisfies the accounting.
GENERIC_BASENAMES = {
    "__init__.py",
    "index.ts",
    "index.tsx",
    "index.js",
    "index.html",
    "SKILL.md",
    "README.md",
    "conftest.py",
    "package.json",
    "types.ts",
    "utils.ts",
    "utils.py",
}


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


def area_of(path):
    """The reviewer-sized unit a path belongs to; mirrors the awk in pr-scope.yml.

    ``src/kiro_crew/a/b/c.py`` -> ``src/kiro_crew/a``; ``website/src/a/b.ts`` ->
    ``website/src/a``; anything else -> its first path component (a top-level file
    is its own area).

    One deliberate difference from the workflow: it excludes
    ``src/kiro_crew/_vendor/**`` and ``temp-screenshots/**`` from its line COUNT,
    while this accounting keeps them. A stray vendor or screenshot edit is exactly
    the kind of change a body should have to name.
    """
    parts = path.split(GIT_SEP)
    if len(parts) >= 3 and (parts[:2] == ["src", "kiro_crew"] or parts[:2] == ["website", "src"]):
        return GIT_SEP.join(parts[:3])
    return parts[0]


def changed_paths(name_status):
    """Paths touched by a ``git diff --name-status`` listing (renames give both sides)."""
    paths: list[str] = []
    for line in name_status.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        paths.extend(c for c in cols[1:] if c)
    return paths


def _named(needle, body):
    """True when ``needle`` occurs in ``body`` as a whole path token.

    A path character may precede it (``a/b/c`` names ``b/c``) and a ``/`` may follow
    it (a deeper path inside an area still names the area), but a word, dot or
    dash character on either side means a different name that merely shares a
    prefix or suffix (a sentence-ending ``.`` after it is fine): ``src/kiro_crew/chat`` is not named by
    ``src/kiro_crew/chat_runner.py``, ``chat.py`` is not named by ``mychat.py`` or
    ``chat.py.bak``, and the area ``test`` is not named by the heading ``Tests``.
    """
    pattern = r"(?<![\w.-])" + re.escape(needle) + r"(?![\w-]|\.\w)"
    return re.search(pattern, body) is not None


def _path_tails(path):
    """Every ``dir/.../file`` suffix of a path that still carries a directory."""
    parts = path.split(GIT_SEP)
    return [GIT_SEP.join(parts[i:]) for i in range(len(parts) - 1)]


def unaccounted_areas(paths, body):
    """Areas of ``paths`` the body never names. Returns [(area, [paths...]), ...]."""
    by_area: dict[str, list[str]] = {}
    for p in paths:
        by_area.setdefault(area_of(p), []).append(p)
    basename_counts: dict[str, int] = {}
    for p in paths:
        b = os.path.basename(p)
        basename_counts[b] = basename_counts.get(b, 0) + 1

    missing = []
    for area in sorted(by_area):
        files = by_area[area]
        if _named(area, body):
            continue
        named = False
        for p in files:
            if any(_named(tail, body) for tail in _path_tails(p)):
                named = True
                break
            b = os.path.basename(p)
            if b not in GENERIC_BASENAMES and basename_counts[b] == 1 and _named(b, body):
                named = True
                break
        if not named:
            missing.append((area, files))
    return missing


def what_changed_prose(body):
    """The ``## What changed`` section with fences, table rows and image lines removed.

    Returns None when the body has no such section (template absent or renamed).
    """
    m = re.search(r"^## +What changed[^\n]*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    if not m:
        return None
    section = re.sub(r"^```.*?^```[ \t]*$", "", m.group(1), flags=re.MULTILINE | re.DOTALL)
    kept = []
    for line in section.splitlines():
        s = line.strip()
        if s.startswith("|") or s.startswith("!["):
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            continue
        kept.append(line)
    return "\n".join(kept)


def word_count(text):
    return len(text.split())


def body_path(gitdir):
    """The one file ``--check-body`` reads: ``BODY_FILENAME`` inside the git directory."""
    return os.path.join(gitdir, BODY_FILENAME)


def check_body(body, name_status, soft_words=SOFT_WORDS):
    """Run both checks; print findings; return the exit code (0 or 20)."""
    print()
    print("=== Body check ===")
    missing = unaccounted_areas(changed_paths(name_status), body)
    for area, files in missing:
        print("UNACCOUNTED {} - body names none of:".format(area))
        for f in files:
            print("    " + f)
    prose = what_changed_prose(body)
    if prose is None:
        print("WARN: no '## What changed' section found - length check skipped")
    else:
        n = word_count(prose)
        if n > soft_words:
            print(
                "WARN: What changed is {} words of prose (soft limit {}) - "
                "compress, or split the PR; not a gate".format(n, soft_words)
            )
        else:
            print("What changed: {} words of prose (soft limit {})".format(n, soft_words))
    if missing:
        print(
            "{} unaccounted area(s): name the change in the body, or drop it from the diff".format(
                len(missing)
            )
        )
        return 20
    print("every changed area is named in the body")
    return 0


def parse_args(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("base", nargs="?", default="")
    ap.add_argument("--check-body", action="store_true")
    return ap.parse_args(argv[1:])


def main(argv):
    try:
        args = parse_args(argv)
    except SystemExit:
        return 2

    if run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
        err("ERROR: not inside a git repository (or git not found).")
        return 2

    base = args.base
    if not base:
        sym = run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])[
            1
        ].strip()
        base = sym[len("origin/") :] if sym.startswith("origin/") else ""
    if not base:
        base = "main"

    if run(["git", "rev-parse", "--verify", "--quiet", "origin/" + base])[0] != 0:
        err("ERROR: origin/{0} not found - run: git fetch origin {0}".format(base))
        return 2

    body = None
    if args.check_body:
        gitdir = run(["git", "rev-parse", "--absolute-git-dir"])[1].strip()
        if not gitdir:
            err("ERROR: git rev-parse --absolute-git-dir returned nothing")
            return 2
        path = body_path(gitdir)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError as exc:
            err("ERROR: cannot read body file {}: {}".format(path, exc))
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

    if body is not None:
        return check_body(body, ns)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""diff_signals.py - change-signal inventory for the description-reconcile step.

Lists changed files by status vs the base branch and flags notable structural
signals so the PR body can be checked for completeness against what the diff
actually does. Stdlib only; portable across OSes.

With ``--check-body`` it also reads the PR body the skill writes --
``<git-dir>/prepare-pr-body.md``, with ``<git-dir>`` from
``git rev-parse --absolute-git-dir`` -- and runs three checks on it:

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
* **Length (hard).** The prose of ``## What changed`` -- fenced blocks, table rows
  and image lines excluded -- is counted against ``WORD_LIMIT``, the length the
  contract's "three short paragraphs at most" come to. Over it is exit 21. The
  accounting check already guarantees nothing is hidden, so a cap cannot cut a
  true fact -- only a restated one: the body says what changed for the reader
  and why; the diff is the evidence. Table rows there are bounded too, by
  ``TABLE_ROW_LIMIT`` -- the "eight rows" ceiling ``references/body-picture.md``
  sets for a picture -- so a recital cannot move into a table to dodge the count.
  A body with no ``## What changed`` section is left to the template check below.
* **Template (hard).** The repo's own ``.github/scripts/pr-description-check.sh``
  -- the script CI's PR Hygiene runs -- is read from ``origin/<base>``, never from
  the checkout, so a branch cannot weaken its own check, and run on the body.
  A failure prints that script's message; that is exit 22. A base without the
  script (another repo) skips it silently.

All checks run and print before any exit; when several breach, the lowest code wins.

Size and age signals are printed, never an exit code. ``! SIZE`` appears at
``SIZE_HINT_FILES`` changed files and says more at ``SIZE_STOP_FILES``, where CI's
``PR Size`` check (``.github/scripts/pr_size.py``) wants an owner's approval once it
is enforced. ``--age`` adds ``! AGE`` from ``gh pr view`` when the pull request has
been open ``AGE_HOURS`` hours; it is opt-in because it is the one call that leaves
the machine.

Usage:  python3 diff_signals.py [base-branch] [--check-body] [--age]
Exit:   0 printed / body passes | 20 unaccounted area(s) | 21 What changed too long |
        22 template sections missing | 2 environment error
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

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

# Three short paragraphs at most, with room for a cross-cutting change; what it
# stops is the 800-word per-file recital. SKILL.md's PR description contract
# states the same number next to its paragraph rule; a test keeps the two equal.
WORD_LIMIT = 500

# Table rows (header and |---| separator not counted) allowed in What changed,
# summed over every table there. references/body-picture.md's "eight rows is the
# ceiling" for a picture; a test keeps the two equal.
TABLE_ROW_LIMIT = 8

# Size and age signals. Printed, never an exit code. A test keeps these equal to the
# thresholds in .github/scripts/pr_size.py, which is what CI's PR Size check judges
# (files, not lines: a 5,000-line split is usually fine, an 80-file change is not).
SIZE_HINT_FILES = 50
SIZE_STOP_FILES = 80
AGE_HOURS = 168

# git prints every path with forward slashes, on every OS. These helpers split
# and join git's own output, never filesystem paths, so the separator is git's.
GIT_SEP = "/"

# The one body file --check-body reads, relative to `git rev-parse --absolute-git-dir`.
BODY_FILENAME = "prepare-pr-body.md"

# CI's template check, read from origin/<base> by --check-body.
TEMPLATE_CHECK = ".github/scripts/pr-description-check.sh"

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


def size_signal(file_count):
    """The ``! SIZE`` line for a diff of ``file_count`` files, or None under the hint."""
    if file_count >= SIZE_STOP_FILES:
        return (
            "! SIZE: {} files (CI's PR Size limit is {}) - an owner approves with "
            "`/pr-size approve: <reason>`; or split along a seam, or land it today".format(
                file_count, SIZE_STOP_FILES
            )
        )
    if file_count >= SIZE_HINT_FILES:
        return (
            "! SIZE: {} files (hint at {}, PR Size limit {}) - split along a seam "
            "or land it today".format(file_count, SIZE_HINT_FILES, SIZE_STOP_FILES)
        )
    return None


def hours_open(created_at, now):
    """Whole hours from an ISO-8601 ``created_at`` (``Z`` suffix allowed) to ``now``."""
    text = created_at.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    created = datetime.fromisoformat(text)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((now - created).total_seconds() // 3600))


def age_signal(hours):
    """The ``! AGE`` line for a pull request open ``hours`` hours, or None."""
    if hours >= AGE_HOURS:
        return (
            "! AGE: open {} h ({}+ days) - merge, split or close; waiting for review "
            "is not your fault, so ask an owner which".format(hours, AGE_HOURS // 24)
        )
    return None


def pr_created_at():
    """``createdAt`` of this branch's pull request, or None (no gh, no PR, no network)."""
    rc, out, _ = run(["gh", "pr", "view", "--json", "createdAt", "--jq", ".createdAt"])
    return out.strip() if rc == 0 and out.strip() else None


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


def _what_changed_unfenced(body):
    """The ``## What changed`` section with fenced blocks removed, or None if absent."""
    m = re.search(r"^## +What changed[^\n]*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    if not m:
        return None
    return re.sub(r"^```.*?^```[ \t]*$", "", m.group(1), flags=re.MULTILINE | re.DOTALL)


_TABLE_SEPARATOR = re.compile(r"^\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")


def what_changed_table_rows(body):
    """Table rows in ``## What changed``, outside fences; headers and separators excluded.

    Each ``|---|`` separator marks the row above it as a header. Returns None when
    the body has no such section.
    """
    section = _what_changed_unfenced(body)
    if section is None:
        return None
    rows = 0
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if _TABLE_SEPARATOR.match(s):
            rows -= 1  # the line above was the header, not a row
        else:
            rows += 1
    return max(rows, 0)


def what_changed_prose(body):
    """The ``## What changed`` section with fences, table rows and image lines removed.

    Returns None when the body has no such section (template absent or renamed).
    """
    section = _what_changed_unfenced(body)
    if section is None:
        return None
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


def check_body(body, name_status, word_limit=WORD_LIMIT):
    """Run both checks; print findings; return the exit code (0, 20 or 21)."""
    print()
    print("=== Body check ===")
    missing = unaccounted_areas(changed_paths(name_status), body)
    for area, files in missing:
        print("UNACCOUNTED {} - body names none of:".format(area))
        for f in files:
            print("    " + f)
    prose = what_changed_prose(body)
    too_long = False
    if prose is None:
        # The template check (exit 22) already fails this body; not a second gate.
        print("WARN: no '## What changed' section found - length check skipped")
    else:
        rows = what_changed_table_rows(body)
        if rows > TABLE_ROW_LIMIT:
            too_long = True
            print(
                "TOO MANY ROWS: What changed has {} table rows (limit {}) - a picture shows "
                "only the delta; move the walkthrough out, the diff is the evidence".format(
                    rows, TABLE_ROW_LIMIT
                )
            )
        n = word_count(prose)
        if n > word_limit:
            too_long = True
            print(
                "TOO LONG: What changed is {} words of prose (limit {}) - compress: say what "
                "changed for the reader and why; the diff is the evidence".format(n, word_limit)
            )
        else:
            print("What changed: {} words of prose (limit {})".format(n, word_limit))
    if missing:
        print(
            "{} unaccounted area(s): name the change in the body, or drop it from the diff".format(
                len(missing)
            )
        )
        return 20
    print("every changed area is named in the body")
    if too_long:
        return 21
    return 0


def template_check(body, base):
    """Run the base ref's ``TEMPLATE_CHECK`` on ``body``; return 0 or 22.

    Absent on the base (another repo) is a silent skip.
    """
    rc, script, _ = run(["git", "show", "origin/{}:{}".format(base, TEMPLATE_CHECK)])
    if rc != 0:
        return 0
    bash = shutil.which("bash")
    if not bash:
        print("WARN: bash not found - template check skipped; CI still runs it")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "check.sh")
        out_path = os.path.join(tmp, "out")
        with open(script_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        env = dict(os.environ, PR_BODY=body, PR_DRAFT="false", GITHUB_OUTPUT=out_path)
        env.setdefault("GITHUB_REPOSITORY", "")
        try:
            subprocess.run([bash, script_path], env=env, capture_output=True)
            with open(out_path, encoding="utf-8", errors="replace") as fh:
                out = fh.read()
        except OSError as exc:
            print("WARN: cannot run {} ({}) - template check skipped".format(TEMPLATE_CHECK, exc))
            return 0
    if "conclusion=failure" not in out.splitlines():
        print("PR template: required sections present")
        return 0
    m = re.search(r"^title=(.*)$", out, re.MULTILINE)
    print("TEMPLATE: " + (m.group(1) if m else "the body fails " + TEMPLATE_CHECK))
    # The summary's bullets name what is missing; its prose addresses a PR page.
    for line in out.splitlines():
        if line.startswith("- "):
            print("    " + line)
    return 22


def parse_args(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("base", nargs="?", default="")
    ap.add_argument("--check-body", action="store_true")
    ap.add_argument("--age", action="store_true")
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
    # One name-status row per changed file (a rename is one row), the count GitHub
    # reports as changed_files and PR Size judges.
    size_line = size_signal(len([row for row in ns.splitlines() if row.strip()]))
    if size_line:
        print(size_line)
        any_flag = True
    if args.age:
        created = pr_created_at()
        try:
            age_line = (
                age_signal(hours_open(created, datetime.now(timezone.utc))) if created else None
            )
        except ValueError:  # a createdAt that is not a timestamp is no signal
            age_line = None
        if age_line:
            print(age_line)
            any_flag = True
    if not any_flag:
        print("(no notable structural signals - still describe the behavior changes)")

    if body is not None:
        rc = check_body(body, ns)
        tpl = template_check(body, base)
        return rc or tpl
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

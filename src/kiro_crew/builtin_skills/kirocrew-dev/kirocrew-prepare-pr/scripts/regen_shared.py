#!/usr/bin/env python3
"""regen_shared.py - regenerate the shared generated files last, on a fresh rebase.

A few files are rebuilt by a tool and changed by many pull requests at once: the
find_ui index, the file-length baseline, the black baseline. When two branches
touch one, the rebase conflicts on a file nobody should merge by hand. The remedy
is to take the base side of the conflict and run the generator on the rebased tree,
so the file is derived from the final tree instead of reconciled line by line.
(RFC D6; recipe in ``references/split-and-regen.md``.)

Run it AFTER ``git rebase`` and BEFORE the gates and the final push. For each entry
in ``REGEN`` whose OUTPUT files or INPUT paths this branch (or the base since the
merge base) changed, it runs the generator with no shell, then compares the file
bytes before and after. Inputs matter because a conflict in a generated file is
resolved by taking the base side: the output then equals base and looks untouched,
while the branch's own inputs have made it stale.

Only files with a generator are here. Four hot files have none and are listed in
``NOT_COVERED``; this script prints a reminder when the branch touches one and does
not invent a generator for it.

A generator that rewrites a file outside its entry's ``files`` is an error, not a
quiet extra edit: the amend would carry a change nobody reviewed.

Usage:  python3 regen_shared.py [base-branch] [--list]
Exit:   0 nothing stale, or the generators changed nothing | 20 rewrote files
        (amend them: ``push_guard.py --amend -- <files>``) | 2 environment error
        (not a repo, base missing, tool missing, a generator failed, a file
        outside the list changed)

Stdlib only; Python 3.10+.
"""

import fnmatch
import os
import shlex
import shutil
import subprocess
import sys

# One entry per generator that exists today. ``files`` are the repo-relative paths it
# writes (and the only ones it may write); ``requires`` are paths that must exist for
# the entry to apply (another repo, or a checkout without the tool, skips it); ``argv``
# runs with no shell, ``python3`` meaning this interpreter. ``inputs`` are fnmatch
# patterns (``*`` also crosses ``/``) for repo-relative paths whose change can make
# ``files`` stale. ``listed_in_output`` marks a baseline whose rows are paths: its
# generator only prunes rows, so a changed path matters only when the baseline lists it.
# ``note`` is one honest line about cost or scope.
REGEN = [
    {
        "name": "find_ui index",
        "files": [
            "src/kiro_crew/docs/ui-index.generated.json",
            "website/src/uiLocations/guidePlans.gen.ts",
        ],
        "requires": ["website/scripts/gen-ui-index.mjs"],
        "inputs": [
            "website/src/*",
            "website/scripts/*",
            "src/kiro_crew/docs/settings-registry.generated.json",
        ],
        "argv": ["npm", "--prefix", "website", "run", "gen:ui"],
        "note": "needs `npm ci` in website/ first; the build's `--check` fails when it is stale",
    },
    {
        "name": "file-length baseline",
        "files": [".github/file-line-cap-baseline.txt"],
        "requires": ["scripts/check_file_line_cap.py"],
        "inputs": [],
        "listed_in_output": True,
        "argv": ["python3", "scripts/check_file_line_cap.py", "--update-baseline"],
        "note": "only deletes rows and lowers counts for files this change touches; never adds",
    },
    {
        "name": "black baseline",
        "files": [".github/black-baseline.txt"],
        "requires": ["scripts/check_black_formatting.py"],
        "inputs": [],
        "listed_in_output": True,
        "argv": ["python3", "scripts/check_black_formatting.py", "--update-baseline"],
        "note": "runs `black --check` over the whole tree to find rows now clean (minutes); only deletes rows",
    },
]

# Hot files with no generator. They are edited by hand; a gate only fails on them.
NOT_COVERED = {
    "docs/feature-map/README.md": "hand-written rows; scripts/check_feature_map.py only fails",
    "docs/architecture/mcp.md": "hand-written",
    "test/test_security_gate_liveness.py": "_PACKAGE_LINE_BUDGET is a hand-counted number",
    "website/src/i18n/en.context.json": "hand-written translator context",
}

GENERATOR_TIMEOUT_SECONDS = 1800

# The sibling script the amend hint names. Absolute, because the author runs this from
# the repository root and push_guard.py lives in the skill's scripts/ directory.
PUSH_GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_guard.py")


def err(msg):
    sys.stderr.write(msg + "\n")


def run(args, cwd=None):
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        return p.returncode, p.stdout, p.stderr
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def _git_lines(root, *args):
    rc, out, _ = run(["git", "-C", root, *args])
    return None if rc != 0 else [line for line in out.splitlines() if line.strip()]


def baseline_paths(root, relpath):
    """Paths named by a baseline file: the last token of each non-comment line."""
    try:
        with open(os.path.join(root, relpath), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return set()
    return {ln.split()[-1] for ln in lines if ln.strip() and not ln.lstrip().startswith("#")}


def stale_entries(entries, mine, theirs, root=None):
    """Entries whose output files or generator inputs changed on the branch or the base.

    Outputs alone are not enough: after "take the base side" on a conflicted output,
    the file equals base and is absent from the diff, yet the branch's inputs have
    made it stale. A baseline that only prunes rows (``listed_in_output``) is stale
    when a changed path is one of its rows; ``root`` is where it is read.
    """
    touched = sorted(set(mine) | set(theirs))

    def hit(entry):
        patterns = list(entry.get("inputs", []))
        listed = set()
        if entry.get("listed_in_output") and root is not None:
            listed = baseline_paths(root, entry["files"][0])
        return any(
            path in entry["files"]
            or path in listed
            or any(fnmatch.fnmatchcase(path, pat) for pat in patterns)
            for path in touched
        )

    return [e for e in entries if hit(e)]


def untracked_or_modified(root):
    """``{path: bytes-or-None}`` for every path git reports changed, to compare around a run."""
    rc, out, _ = run(["git", "-C", root, "status", "--porcelain", "-z", "--untracked-files=all"])
    if rc != 0:
        return None
    seen: dict[str, bytes | None] = {}
    for item in out.split("\0"):
        if len(item) < 4:
            continue
        path = item[3:]
        try:
            with open(os.path.join(root, path), "rb") as fh:
                seen[path] = fh.read()
        except OSError:
            seen[path] = None
    return seen


def _argv(entry):
    argv = list(entry["argv"])
    if argv[0] == "python3":
        argv[0] = sys.executable
    return argv


def run_entry(root, entry):
    """Run one generator. Returns (error message or None, files it rewrote)."""
    argv = _argv(entry)
    # Launch the path `which` found, not the bare name: with no shell, Windows only
    # finds `npm.cmd` through the full path (`npm` alone is WinError 2).
    tool = shutil.which(argv[0])
    if tool is None and os.path.isfile(argv[0]):
        tool = argv[0]
    if tool is None:
        return "{}: tool `{}` not found on PATH".format(entry["name"], entry["argv"][0]), []
    argv[0] = tool
    before = untracked_or_modified(root)
    if before is None:
        return "git status failed in {}".format(root), []
    try:
        p = subprocess.run(
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GENERATOR_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "{}: {}".format(entry["name"], exc), []
    if p.returncode != 0:
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-8:])
        return (
            "{}: `{}` exited {}\n{}".format(
                entry["name"], " ".join(entry["argv"]), p.returncode, tail
            ),
            [],
        )
    after = untracked_or_modified(root)
    if after is None:
        return "git status failed in {}".format(root), []
    changed = sorted(
        path
        for path in set(before) | set(after)
        if before.get(path, "gone") != after.get(path, "gone")
    )
    outside = [path for path in changed if path not in entry["files"]]
    if outside:
        return (
            "{}: the generator also changed files outside its list: {}. Inspect with "
            "`git status` and `git diff`; do not amend them".format(
                entry["name"], ", ".join(outside)
            ),
            [],
        )
    return None, changed


def regen(root, base, entries):
    """Do the work. Returns (exit code, printed lines)."""
    lines = []
    if _git_lines(root, "rev-parse", "--is-inside-work-tree") is None:
        return 2, ["ERROR: not inside a git repository (or git not found)."]
    if _git_lines(root, "rev-parse", "--verify", "--quiet", "origin/" + base) is None:
        return 2, ["ERROR: origin/{0} not found - run: git fetch origin {0}".format(base)]
    merge_base = _git_lines(root, "merge-base", "origin/" + base, "HEAD")
    if not merge_base:
        return 2, ["ERROR: no merge base between origin/{} and HEAD".format(base)]
    mb = merge_base[0]
    # --no-renames lists both sides of a rename, so a baseline row for the old path is found.
    mine = _git_lines(root, "diff", "--name-only", "--no-renames", mb + "..HEAD")
    theirs = _git_lines(root, "diff", "--name-only", "--no-renames", mb + "..origin/" + base)
    if mine is None or theirs is None:
        return 2, ["ERROR: git diff failed"]
    # A worktree edit counts as this branch's: the author may regenerate before committing.
    dirty = _git_lines(root, "diff", "--name-only", "--no-renames", "HEAD") or []
    mine = sorted(set(mine) | set(dirty))

    applicable = []
    for entry in entries:
        missing = [r for r in entry["requires"] if not os.path.exists(os.path.join(root, r))]
        if missing:
            lines.append("skip {}: {} not in this repository".format(entry["name"], missing[0]))
        else:
            applicable.append(entry)
    todo = stale_entries(applicable, mine, theirs, root)
    if not todo:
        lines.append(
            "no generated shared file or generator input is touched by this branch or the base: nothing to do"
        )

    rewritten = []
    for entry in todo:
        lines.append("regenerating {} ({})".format(entry["name"], " ".join(entry["argv"])))
        error, changed = run_entry(root, entry)
        if error:
            lines.append("ERROR: " + error)
            return 2, lines
        rewritten.extend(changed)
        lines.append("  rewrote: " + ", ".join(changed) if changed else "  already current")

    hand = sorted(f for f in NOT_COVERED if f in mine)
    for f in hand:
        lines.append(
            "HAND: {} has no generator ({}); re-read it against the rebased tree".format(
                f, NOT_COVERED[f]
            )
        )

    if rewritten:
        lines.append(
            "amend what changed: "
            + " ".join(
                shlex.quote(part)
                for part in ["python3", PUSH_GUARD, "--amend", "--", *sorted(set(rewritten))]
            )
        )
        return 20, lines
    return 0, lines


def listing(entries):
    out = [
        "Regenerated, in this order (only when the branch or the base touched the files or their inputs):"
    ]
    for e in entries:
        out.append("  {} - {}".format(e["name"], ", ".join(e["files"])))
        out.append("      {}  [{}]".format(" ".join(e["argv"]), e["note"]))
        out.append(
            "      reruns when it changes: "
            + (", ".join(e["inputs"]) or "a path the baseline lists")
        )
    out.append("Not covered (no generator; edit by hand):")
    for path, why in sorted(NOT_COVERED.items()):
        out.append("  {} - {}".format(path, why))
    return out


def main(argv):
    args = [a for a in argv[1:] if a != "--list"]
    if len(args) > 1 or any(a.startswith("-") for a in args):
        err("usage: regen_shared.py [base-branch] [--list]")
        return 2
    if "--list" in argv[1:]:
        print("\n".join(listing(REGEN)))
        return 0
    root_rc, root_out, _ = run(["git", "rev-parse", "--show-toplevel"])
    if root_rc != 0:
        err("ERROR: not inside a git repository (or git not found).")
        return 2
    root = root_out.strip()
    base = args[0] if args else ""
    if not base:
        sym = run(
            ["git", "-C", root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]
        )[1].strip()
        base = sym[len("origin/") :] if sym.startswith("origin/") else "main"
    code, lines = regen(root, base, REGEN)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

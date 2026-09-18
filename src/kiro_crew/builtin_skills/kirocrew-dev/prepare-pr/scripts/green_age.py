#!/usr/bin/env python3
"""green_age.py - has this head's CI green expired because the base moved?

A pull request's green is a verdict about ``refs/pull/<N>/merge`` at the moment
the run was triggered, not about the branch. Nothing re-derives it when the base
branch moves, so a head that passed hours ago can be merged against a base whose
new commits its tests never saw. That is not hypothetical: a PR ran CI once,
waited about ten hours for review, and merged with no re-run after two other
PRs had added tests that the merged change makes fail. The red surfaced on an
unrelated PR two hours later.

This script answers the one question that decides whether the wait invalidated
the verdict: **did the base move in files this PR also touches?**

* ``moved``  - ``git diff --name-only <merge-base> origin/<base>``, the files the
  base gained or changed since the commit this head was tested on top of.
  prepare-pr rebases before every push, so the merge base of HEAD and
  ``origin/<base>`` IS the base tip the green was measured on.
* ``mine``   - the PR's own changed files, from ``gh pr view --json files`` with
  ``--pr``, otherwise ``git diff --name-only origin/<base>...HEAD``.

An overlap between the two is reported in one of four classes, most direct
first. Each class exists because it has been observed to break a green head:

1. ``same-file``  - both sides edited one file. Textual conflict or silent
   semantic collision.
2. ``same-dir``   - the two files share a parent directory that names a MODULE.
   A parent that only describes layout (``test/``, ``scripts/``, ``.github/``) does
   not count: those are flat directories holding thousands of unrelated files, so
   treating one as a module would make every moved test file collide with every PR
   that touches any test.
3. ``import``     - a moved ``.py`` imports a module this PR changed, absolute or
   relative. The moved file is a caller whose behaviour this PR redefines.
4. ``test-stem``  - a moved ``test_<stem>.py`` whose stem prefix names a
   directory this PR changed. This is the incident's own shape: new tests in
   ``test/test_ledger_*.py`` against a changed ``src/kiro_crew/ledger/``, which
   share no path component at all.

**This is information for the merger, never a merge gate.** It runs client-side,
during the review wait, and costs anything only on a PR whose files actually
collide. It adds no required check and slows no merge.

Usage:  python3 green_age.py [--base BRANCH] [--pr N]
Exit:   0 FRESH (no overlap) | 30 STALE (rebase and re-run) | 2 environment error
"""

import argparse
import re
import shutil
import subprocess
import sys

# Exit codes, named so the callers in SKILL.md and pr_status.py cannot drift
# from the values here.
EXIT_FRESH = 0
EXIT_STALE = 30
EXIT_ENV = 2

# Directory components that name a layout, not a module. A parent whose last
# component is one of these is not evidence that two files belong together --
# `test/` is one flat directory holding thousands of unrelated files, so treating
# it as a module directory makes every moved test file collide with every PR that
# touches any test. The same names also disqualify a test stem: a stem matching
# `src` says nothing about which code the test covers.
GENERIC_DIR_NAMES = frozenset(
    {
        "src",
        "test",
        "tests",
        "scripts",
        "website",
        "lib",
        "app",
        "docs",
        "packages",
        "public",
        "assets",
        ".github",
    }
)

# `from X import a, b` and `import X.Y, Z`, with optional indentation: an import
# inside a function body binds the same module as one at column 0.
_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+(?P<from>\.*[A-Za-z_][\w.]*)[ \t]+import[ \t]+(?P<names>.+)"
    r"|import[ \t]+(?P<plain>[A-Za-z_][\w.,\t ]*))$",
    re.MULTILINE,
)

# git prints every path with forward slashes, on every OS. Every split and join
# below is applied to git's own output -- a repository path, never a filesystem
# path -- so the separator is git's and pathlib would be the wrong tool: it would
# rewrite a repository path into a host path on Windows. Named so that is legible
# at each call site, the way diff_signals.py already does it.
GIT_SEP = "/"

# How many overlap entries the human line prints before it summarises the rest.
# The list is one line in a poll cycle's output, not a report.
_OVERLAP_PRINT_CAP = 12


def run(args):
    """Run a command; return (returncode, stdout, stderr) as stripped text.

    Every command this script issues goes through ONE runner, and
    :func:`summarize` takes it as a parameter, so an embedding caller
    (``pr_status.py``) reuses its own audited runner rather than the script
    growing a second, separately-stubbed way to reach git.
    """
    try:
        if args and args[0] == "git":
            resolved = shutil.which("git")
            if resolved:
                args = [resolved] + list(args[1:])
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except OSError as exc:
        return 127, "", "{}: {}".format(args[0], exc)


def err(msg):
    sys.stderr.write(msg + "\n")


class EnvError(Exception):
    """An environment or state problem: freshness could not be determined.

    Raised rather than returned so no caller can mistake "could not tell" for
    "fresh". :func:`summarize` converts it into ``ok: False`` plus a reason, and
    :func:`main` into exit 2.
    """


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def dotted_module_paths(paths):
    """Dotted import paths for the ``.py`` files in ``paths``.

    A leading ``src/`` is dropped because it is a source root, not a package,
    and ``__init__.py`` resolves to its package rather than to a submodule that
    no import names.
    """
    dotted = set()
    source_root = "src" + GIT_SEP
    for path in paths:
        if not path.endswith(".py"):
            continue
        rel = path[len(source_root) :] if path.startswith(source_root) else path
        parts = rel[: -len(".py")].split(GIT_SEP)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and all(re.match(r"^[A-Za-z_]\w*$", p) for p in parts):
            dotted.add(".".join(parts))
    return dotted


def _package_of(path):
    """The dotted package a ``.py`` file's own relative imports resolve against.

    ``src/kiro_crew/knowledge/retrieval.py`` -> ``kiro_crew.knowledge``. Returns
    None when the path is not a ``.py`` file whose parts are all identifiers, since
    a relative import inside it cannot be resolved either.
    """
    if not path.endswith(".py"):
        return None
    source_root = "src" + GIT_SEP
    rel = path[len(source_root) :] if path.startswith(source_root) else path
    # Dropping the last part gives the package either way: a module's package is
    # its directory, and __init__.py's own package is the directory it defines.
    parts = rel[: -len(".py")].split(GIT_SEP)[:-1]
    if not parts or not all(re.match(r"^[A-Za-z_]\w*$", p) for p in parts):
        return None
    return ".".join(parts)


def _resolve_relative(base, package):
    """``..x`` inside package ``a.b.c`` -> ``a.b.x``; "" when it cannot resolve.

    One leading dot is the file's own package, each further dot climbs one level,
    exactly as Python resolves it. Climbing past the top is not a real import, so
    it resolves to nothing rather than to a guess.
    """
    if not package:
        return ""
    dots = len(base) - len(base.lstrip("."))
    tail = base[dots:]
    parts = package.split(".")
    # Climbing to or past the root is not a real import, so it resolves to
    # nothing rather than to a top-level guess.
    if dots - 1 >= len(parts):
        return ""
    anchor = parts[: len(parts) - (dots - 1)]
    return ".".join(anchor + ([tail] if tail else []))


def imported_modules(text, path=""):
    """Every dotted name the ``import`` statements in ``text`` bind.

    ``from X import a, b`` yields ``X``, ``X.a`` and ``X.b``: which of the three
    a name refers to is not decidable from the text, and for an overlap test any
    of them is enough.

    A RELATIVE import is resolved against ``path``'s own package when one is
    given, because skipping it is a false FRESH -- the dangerous direction.
    ``src/kiro_crew/knowledge/retrieval.py`` writing ``from .._sqlite_compat
    import x`` binds ``kiro_crew._sqlite_compat``, and a PR changing that module
    is a real overlap that a skipped import reports as no overlap at all. With no
    ``path`` (a bare text probe) a relative target cannot be resolved and is
    skipped, which is why the callers always pass one.
    """
    found = set()
    package = _package_of(path) if path else None
    for m in _IMPORT_RE.finditer(text or ""):
        if m.group("plain"):
            for chunk in m.group("plain").split(","):
                name = chunk.strip().split(" as ")[0].strip()
                if name and not name.startswith("."):
                    found.add(name)
            continue
        base = (m.group("from") or "").strip()
        if not base:
            continue
        if base.startswith("."):
            base = _resolve_relative(base, package)
            if not base:
                continue
        found.add(base)
        names = (m.group("names") or "").strip()
        if names.startswith("*"):
            continue
        for chunk in names.strip("()").split(","):
            leaf = chunk.strip().split(" as ")[0].strip()
            if re.match(r"^[A-Za-z_]\w*$", leaf):
                found.add(base + "." + leaf)
    return found


def _imports_touch(imported, changed_dotted):
    """The changed module an import binds, or "" when none.

    Matching runs in both directions on a dot boundary: importing
    ``kiro_crew.ledger.store`` reaches a changed ``kiro_crew/ledger/__init__.py``
    (package below module), and importing the package ``kiro_crew.ledger``
    reaches a changed ``kiro_crew/ledger/store.py`` (module below package).
    """
    for target in sorted(imported):
        for changed in sorted(changed_dotted):
            if target == changed:
                return changed
            if target.startswith(changed + ".") or changed.startswith(target + "."):
                return changed
    return ""


def test_stem_prefixes(path):
    """Cumulative underscore prefixes of a ``test_<stem>.py`` file's stem.

    ``test/test_ledger_retention.py`` yields ``ledger`` and
    ``ledger_retention``, so the file can be matched against a directory named
    for either.
    """
    name = path.rsplit(GIT_SEP, 1)[-1]
    if not name.startswith("test_") or not name.endswith(".py"):
        return []
    parts = [p for p in name[len("test_") : -len(".py")].split("_") if p]
    return ["_".join(parts[: i + 1]) for i in range(len(parts))]


def changed_dir_names(paths):
    """Non-generic directory components of ``paths``, as a set of names."""
    names = set()
    for path in paths:
        for component in path.split(GIT_SEP)[:-1]:
            if component and component not in GENERIC_DIR_NAMES:
                names.add(component)
    return names


def _parent(path):
    return path.rsplit(GIT_SEP, 1)[0] if GIT_SEP in path else ""


def _module_dir(path):
    """The parent directory of ``path`` when it names a module, else ``""``.

    A parent whose last component only describes layout (``test/``, ``scripts/``,
    ``.github/``) is not a module: those directories are flat and hold thousands of
    unrelated files, so two files sharing one says nothing about either.
    """
    parent = _parent(path)
    if not parent or parent.rsplit(GIT_SEP, 1)[-1] in GENERIC_DIR_NAMES:
        return ""
    return parent


def classify_overlap(moved, mine, read_moved):
    """Classify every moved file that overlaps ``mine``.

    ``read_moved(path)`` returns the moved file's text on the base branch, or
    ``""`` when it cannot be read (the base deleted it, or it is not text). It
    is only ever called for a ``.py`` file that reached the ``import`` class, so
    a fresh PR pays for no blob reads at all.

    Returns a list of ``{"moved", "mine", "class"}`` dicts, ordered by moved
    path so two runs on the same pair of commits print the same line.
    """
    mine_set = set(mine)
    mine_by_dir: dict[str, str] = {}
    for path in mine:
        module_dir = _module_dir(path)
        if module_dir:
            mine_by_dir.setdefault(module_dir, path)
    changed_dotted = dotted_module_paths(mine)
    dir_names = changed_dir_names(mine)

    overlaps = []
    for path in sorted(set(moved)):
        if path in mine_set:
            overlaps.append({"moved": path, "mine": path, "class": "same-file"})
            continue
        sibling = mine_by_dir.get(_module_dir(path)) if _module_dir(path) else ""
        if sibling:
            overlaps.append({"moved": path, "mine": sibling, "class": "same-dir"})
            continue
        if path.endswith(".py") and changed_dotted:
            hit = _imports_touch(imported_modules(read_moved(path), path), changed_dotted)
            if hit:
                overlaps.append({"moved": path, "mine": hit, "class": "import"})
                continue
        stem_hit = next((p for p in test_stem_prefixes(path) if p in dir_names), "")
        if stem_hit:
            overlaps.append({"moved": path, "mine": stem_hit, "class": "test-stem"})
    return overlaps


def _rev_parse(ref, runner=None):
    rc, out, _ = (runner or run)(["git", "rev-parse", "--verify", "--quiet", ref])
    if rc != 0 or not out:
        raise EnvError("cannot resolve {} in this checkout".format(ref))
    return out.split()[0]


def _fetch_base(base, runner=None):
    """Fetch ``origin/<base>``; a failure is an environment error, never fresh.

    The explicit refspec updates the remote-tracking ref whatever the clone's
    configured ``remote.origin.fetch`` is, and the leading ``+`` accepts a
    non-fast-forward update after an upstream force-push.

    git's stderr is deliberately NOT surfaced. It is free text that can carry a
    bare credential from a remote helper or a credential helper's error message,
    and push_guard.py's own history shows that shape enumeration does not close
    that class. The operator's own terminal is where the full error belongs.
    """
    refspec = "+refs/heads/{}:refs/remotes/origin/{}".format(base, base)
    rc, _, _ = (runner or run)(["git", "fetch", "--quiet", "origin", refspec])
    if rc != 0:
        raise EnvError(
            "git fetch origin {} failed, so origin/{} may be stale and freshness "
            "cannot be determined (run git fetch yourself to see the error)".format(base, base)
        )


def _pr_files(pr, runner=None):
    if not re.match(r"^[0-9]+$", str(pr)):
        raise EnvError("--pr takes a PR number, got: {!r}".format(pr))
    rc, out, _ = (runner or run)(
        ["gh", "pr", "view", str(pr), "--json", "files", "-q", ".files[].path"]
    )
    if rc != 0:
        raise EnvError("gh pr view {} --json files failed (is gh authenticated?)".format(pr))
    return _lines(out)


def summarize(base="main", pr="", head="HEAD", runner=None):
    """Measure this head's green against today's ``origin/<base>``.

    Returns a dict that is always safe to read: ``ok`` False with a ``reason``
    when freshness could not be determined, never a bare "fresh".
    """
    _run = runner or run
    result = {
        "ok": False,
        "base": base,
        "ci_base": "",
        "base_head": "",
        "commits": 0,
        "moved_files": 0,
        "pr_files": 0,
        "overlap": [],
        "stale": False,
        "reason": "",
    }
    try:
        if _run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
            raise EnvError("not inside a git repository")
        _fetch_base(base, _run)
        base_ref = "origin/{}".format(base)
        base_head = _rev_parse(base_ref, _run)
        head_sha = _rev_parse(head, _run)
        # The base this head was tested on top of. prepare-pr rebases before
        # every push, so the merge base IS the base tip at trigger time.
        rc, out, _ = _run(["git", "merge-base", head_sha, base_ref])
        if rc != 0 or not out:
            raise EnvError("cannot compute the merge base of {} and {}".format(head, base_ref))
        tested_base = out.split()[0]

        rc, out, _ = _run(["git", "rev-list", "--count", "{}..{}".format(tested_base, base_head)])
        if rc != 0:
            raise EnvError("cannot count commits between the tested base and " + base_ref)
        commits = int(out or "0")

        rc, out, _ = _run(["git", "diff", "--name-only", tested_base, base_head])
        if rc != 0:
            raise EnvError("cannot list the files {} gained since the tested base".format(base_ref))
        moved = _lines(out)

        if pr:
            mine = _pr_files(pr, _run)
        else:
            rc, out, _ = _run(["git", "diff", "--name-only", "{}...{}".format(base_ref, head_sha)])
            if rc != 0:
                raise EnvError("cannot list this branch's own changed files")
            mine = _lines(out)

        def read_moved(path):
            rc, text, _ = _run(["git", "show", "{}:{}".format(base_head, path)])
            return text if rc == 0 else ""

        overlap = classify_overlap(moved, mine, read_moved)
        result.update(
            {
                "ok": True,
                "ci_base": tested_base,
                "base_head": base_head,
                "commits": commits,
                "moved_files": len(moved),
                "pr_files": len(mine),
                "overlap": overlap,
                "stale": bool(overlap),
            }
        )
    except EnvError as exc:
        result["reason"] = str(exc)
    except ValueError as exc:
        result["reason"] = "unexpected git output: {}".format(exc)
    return result


def format_line(summary):
    """The one human line, also the line pr_status.py prints.

    Shaped so a poll cycle's reader sees the verdict, the distance and the
    colliding files without reading anything else.
    """
    if not summary.get("ok"):
        return "green age: unavailable ({})".format(summary.get("reason") or "unknown")
    overlap = summary.get("overlap") or []
    if overlap:
        shown = ["{} ({})".format(o["moved"], o["class"]) for o in overlap[:_OVERLAP_PRINT_CAP]]
        rest = len(overlap) - len(shown)
        if rest > 0:
            shown.append("+{} more".format(rest))
        what = ", ".join(shown)
    else:
        what = "none"
    return "green age: base +{} commits ({} -> {}), overlap: {}".format(
        summary.get("commits", 0),
        (summary.get("ci_base") or "")[:8],
        (summary.get("base_head") or "")[:8],
        what,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Report whether the base moved in files this PR also touches."
    )
    parser.add_argument(
        "--base", default="main", help="Base branch name, without the origin/ prefix."
    )
    parser.add_argument(
        "--pr",
        default="",
        help="Take the changed files from this PR instead of the local branch.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    summary = summarize(base=args.base, pr=args.pr)
    line = format_line(summary)
    if not summary["ok"]:
        err("ERROR: " + line)
        code = EXIT_ENV
    else:
        print(line)
        code = EXIT_STALE if summary["stale"] else EXIT_FRESH
        if summary["stale"]:
            err(
                "STALE: origin/{} gained {} commit(s) touching {} file(s) this PR also "
                "touches, so this head's green was not measured against them. Rebase "
                "onto origin/{}, re-run the scoped tests for the overlapping files, "
                "and push.".format(
                    args.base, summary["commits"], len(summary["overlap"]), args.base
                )
            )
        else:
            print("STATUS: FRESH (the base moved in nothing this PR touches)")
    return code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())

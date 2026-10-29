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
  kirocrew-prepare-pr rebases before every push, so the merge base of HEAD and
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

A STALE answer tells a caller to rebase, and on a base that moves every minute it
can keep saying so for as long as the PR is open -- each rebase discarding a green
the PR had already earned. SKILL.md caps that at ``RESYNC_CAP`` re-syncs and then
hands the PR to a person, so with ``--pr`` this script counts the re-sync comments
the loop itself posted and reports exit ``EXIT_CAP_SPENT`` once the cap is spent.
The count is the PR's own comment history because a loop wakes with no memory:
there is no local state a later cycle could read. It is a TOTAL, not a
consecutive run -- a PR that has needed this many rebases at any point is one a
person should look at -- and it is read only when an overlap was already found, so
a FRESH answer still costs no API call. A count that cannot be read leaves the
verdict at ``EXIT_STALE``, exactly as before: only a proven spent cap diverts.

Usage:  python3 green_age.py [--base BRANCH] [--pr N]
Exit:   0 FRESH (no overlap) | 30 STALE (rebase and re-run)
        | 31 STALE and the re-sync cap is spent (hand the PR to a person)
        | 2 environment error
"""

import argparse
import ast
import functools
import re
import shutil
import subprocess
import sys
import warnings

# Exit codes, named so the callers in SKILL.md and pr_status.py cannot drift
# from the values here.
EXIT_FRESH = 0
EXIT_STALE = 30
# STALE, and this PR has already spent its re-sync budget. A separate code
# because the two call for opposite actions -- 30 means rebase, 31 means stop
# rebasing and hand over -- and a caller that reads prose instead of an exit code
# is how the cap went unenforced in the first place.
EXIT_CAP_SPENT = 31
EXIT_ENV = 2

# How many re-syncs one PR gets before a person takes it. SKILL.md Phase 3 step 5
# states the same number in prose; test_prepare_pr_green_age.py pins the two
# together so neither can drift.
RESYNC_CAP = 3

# The first words of the comment the loop posts after every re-sync (SKILL.md
# Phase 3 step 5 fixes the wording). Counting these is what gives a fresh cycle,
# which remembers nothing, a way to know how many it has already done.
RESYNC_MARKER = "Rebased: main moved in"

# The `authorAssociation` values that place a commenter INSIDE the project, used
# with the author clause to decide whose re-sync record is believable.
# `CONTRIBUTOR` is deliberately absent: it means only that an earlier PR of
# theirs merged, which a drive-by commenter can have too.
#
# These do not PROVE push access -- `MEMBER` is org membership and `COLLABORATOR`
# covers read and triage roles as well -- so an insider who cannot push could
# spend a PR's budget. Establishing push rights would need a
# `/repos/.../collaborators/<login>/permission` call per distinct commenter,
# which is a request per author on a path that runs every poll cycle, to narrow a
# case that needs someone already inside the project and whose outcome is a
# hand-over to a person rather than anything destructive.
WRITE_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")

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

# The LINE reading of a file (see `_import_statements`): `from X import a, b` and
# `import X.Y, Z`, with optional indentation, since an import inside a function
# body -- or inside a child-process script held in a string -- binds the same
# module as one at column 0. The base is either a (possibly relative) dotted name
# or dots alone: a bare `from . import a` / `from .. import a` names no module
# after its dots.
_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+(?P<from>\.*[A-Za-z_][\w.]*|\.+)[ \t]+import[ \t]+(?P<names>.+)"
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
    is a real overlap that a skipped import reports as no overlap at all. A bare
    relative import is the same rule with an empty tail:
    ``src/kiro_crew/taskq/dependency.py`` writing ``from . import adapters``
    binds ``kiro_crew.taskq`` and ``kiro_crew.taskq.adapters``: the named
    submodule is matched like any other, and a name that is not a module -- a
    function the package's ``__init__.py`` defines -- still reaches that
    ``__init__.py`` through the package entry. With no
    ``path`` (a bare text probe) a relative target cannot be resolved and is
    skipped, which is why the callers always pass one.
    """
    found: set[str] = set()
    package = _package_of(path) if path else None
    for base, names in _import_statements(text or ""):
        if base is None:
            found.update(name for name in names if name and not name.startswith("."))
            continue
        if base.startswith("."):
            base = _resolve_relative(base, package)
        if not base:
            continue
        found.add(base)
        # `*` and anything else that is not an identifier names no submodule.
        for leaf in names:
            if re.match(r"^[A-Za-z_]\w*$", leaf):
                found.add(base + "." + leaf)
    return found


@functools.lru_cache(maxsize=256)
def _import_statements(text):
    """``(base, names)`` for every import ``text`` makes, read two ways at once.

    ``base`` is None for a plain ``import a.b, c``, whose ``names`` are the
    modules, and otherwise the ``from`` target spelled with its leading dots.

    The syntax tree (:func:`_parsed_imports`) reads what a line scan misreads:
    a trailing comment, a ``)`` inside one, a statement split across lines. The
    line scan (:func:`_scanned_imports`) reads what the tree cannot: an import
    inside a string a test hands to a child interpreter, which really runs it,
    and every import of a blob that does not parse. Their UNION is used, so
    neither reading can turn an overlap the other sees into a false FRESH; an
    import-shaped line in a docstring over-reports, which is the safe direction.

    A leading byte-order mark is dropped first: ``git show`` keeps it, and it
    would fail the parse and hide the first line from the scan.

    Cached because :func:`_imports_touch` re-reads a facade package's
    ``__init__.py`` for every moved file that imports the package, and a parse
    costs an order of magnitude more than the line scan.
    """
    text = text.lstrip("\ufeff")
    return tuple(_parsed_imports(text) or ()) + tuple(_scanned_imports(text))


def _parsed_imports(text):
    """The import statements in ``text``'s syntax tree, or None when it does not parse.

    It does not parse when the syntax is newer than the running interpreter, the
    blob is truncated, or it nests deeper than the parser's stack (CPython raises
    MemoryError for that one). A blob's compile-time warnings (an invalid escape
    in a string) are not this script's output, so they are silenced.

    Only statement lists are walked: an import is always a statement, and
    visiting every expression node costs ten times as much for nothing.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    statements: list[tuple[str | None, list[str]]] = []
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Import):
            statements.append((None, [alias.name for alias in node.names]))
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            statements.append((base, [alias.name for alias in node.names]))
        # Bodies of functions, classes, branches, loops, `with`, `try` and its
        # handlers, and `match` cases. An expression's `body`/`orelse` (a lambda,
        # a conditional expression) is a single node, never a list.
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            children = getattr(node, field, None)
            if isinstance(children, list):
                pending.extend(children)
    return statements


def _scanned_imports(text):
    """The ``(base, names)`` of :func:`_import_statements`, from a line scan."""

    def leaves(spelled):
        return [chunk.strip().split(" as ")[0].strip() for chunk in spelled.split(",")]

    statements: list[tuple[str | None, list[str]]] = []
    for m in _IMPORT_RE.finditer(text):
        if m.group("plain"):
            statements.append((None, leaves(m.group("plain"))))
            continue
        names = m.group("names").split("#", 1)[0].strip()
        if names.startswith("(") and ")" not in names:
            # A parenthesized import spans lines; its names run to the close paren.
            close = text.find(")", m.end())
            names += re.sub(r"#[^\n]*", "", text[m.end() : close if close != -1 else None])
        statements.append((m.group("from"), leaves(names.strip("()"))))
    return statements


def _package_init_paths(package):
    rel = package.replace(".", GIT_SEP) + GIT_SEP + "__init__.py"
    return ("src" + GIT_SEP + rel, rel)


def _imports_touch(imported, changed_dotted, read=None, _seen=None):
    """The changed module an import binds, or "" when none.

    Matching runs in both directions on a dot boundary: importing
    ``kiro_crew.ledger.store`` reaches a changed ``kiro_crew/ledger/__init__.py``
    (package below module), and importing the package ``kiro_crew.ledger``
    reaches a changed ``kiro_crew/ledger/store.py`` (module below package).
    The package-above-module direction is bounded to the changed module's own
    parent package, and never to a top-level token: every module sits below the
    source-root package, so ``from kiro_crew import X`` would otherwise reach
    every changed file. That form still matches through its ``kiro_crew.X`` name.
    A deeper package still reaches the changed module when its ``__init__.py``,
    read through ``read``, imports toward it: a facade that re-exports a module
    two levels down is a real dependency.
    """
    seen = set() if _seen is None else _seen
    for target in sorted(imported):
        for changed in sorted(changed_dotted):
            if target == changed or target.startswith(changed + "."):
                return changed
            if "." not in target or not changed.startswith(target + "."):
                continue
            if changed.rsplit(".", 1)[0] == target:
                return changed
            if read and (target, changed) not in seen:
                seen.add((target, changed))
                for path in _package_init_paths(target):
                    text = read(path)
                    if text and _imports_touch(imported_modules(text, path), {changed}, read, seen):
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
    is called once per moved ``.py`` file that reaches the ``import`` check, and
    once per package ``__init__.py`` that check follows; each path is read at most
    once per run. A PR that changed no ``.py`` file pays for no blob reads at all.

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
    blobs: dict[str, str] = {}

    def read_once(path):
        if path not in blobs:
            blobs[path] = read_moved(path)
        return blobs[path]

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
            imported = imported_modules(read_once(path), path)
            hit = _imports_touch(imported, changed_dotted, read_once)
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


def resync_selector():
    """The ``jq`` program that counts this PR's re-syncs, as one testable value.

    Named and returned rather than inlined because ``gh`` evaluates it remotely:
    a stubbed runner answers a number without the program ever running, so the
    only way to test what it actually selects is to hand this string to ``jq``.
    """
    # Spelled as plain equality rather than `IN` or `index`: this program is
    # evaluated by whatever jq the installed `gh` carries, and an `index` over a
    # literal list reads `.authorAssociation` with the LIST as input, which is a
    # hard jq error rather than a false match.
    can_push = " or ".join(
        '.authorAssociation == "{}"'.format(assoc) for assoc in WRITE_ASSOCIATIONS
    )
    return (
        ".author.login as $author | [.comments[] | select("
        "(.author.login == $author or {}) "
        'and (.body | startswith("{}")))] | length'
    ).format(can_push, RESYNC_MARKER)


def _resync_count(pr, runner=None):
    """How many re-syncs this PR has already had, or -1 when that cannot be read.

    Counted from the PR's own ``Rebased: main moved in`` comments, because a loop
    cycle wakes with no memory of its earlier ones and there is no local state to
    consult. ``jq`` does the counting so the body text never has to be parsed
    here: a comment body is free-form Markdown and can contain anything,
    including this marker quoted inside someone else's comment, so the match is
    anchored at the start of the body the way the loop writes it.

    Only a comment from inside the project counts: one by the PULL REQUEST
    AUTHOR, or by an account whose ``authorAssociation`` is in
    ``WRITE_ASSOCIATIONS`` (see the bound on what that proves, where it is
    defined). The marker is a published string
    on a public repository, so counting any matching body would let a passer-by
    post three comments and strand the PR on exit 31 with none of its budget
    spent. Those two clauses are both needed and neither is sufficient: pushing
    a rebase to a fork branch requires owning it, so an outside contributor
    driving their own PR is only ever matched by the author clause (their
    ``authorAssociation`` is ``CONTRIBUTOR``, exactly like a stranger's), while
    a maintainer driving a branch they did not author -- an assigned PR, or a
    fork with maintainer edits enabled -- is only ever matched by the
    association clause. A drive-by is neither.

    NEVER raises. An unreadable count must leave the overlap verdict exactly as
    it was rather than converting it into an error, since the overlap is the part
    that was actually measured; :func:`main` keeps ``EXIT_STALE`` on -1.
    """
    if not re.match(r"^[0-9]+$", str(pr)):
        return -1
    rc, out, _ = (runner or run)(
        ["gh", "pr", "view", str(pr), "--json", "author,comments", "-q", resync_selector()]
    )
    if rc != 0:
        return -1
    try:
        return int((out or "").strip())
    except ValueError:
        return -1


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
        # -1 means "not read": no PR number, no overlap to rebase for, or the
        # count could not be fetched. Pre-set so an embedding caller
        # (pr_status.py) can read both keys off any result, including a failed one.
        "resyncs": -1,
        "cap_spent": False,
        "reason": "",
    }
    try:
        if _run(["git", "rev-parse", "--is-inside-work-tree"])[0] != 0:
            raise EnvError("not inside a git repository")
        _fetch_base(base, _run)
        base_ref = "origin/{}".format(base)
        base_head = _rev_parse(base_ref, _run)
        head_sha = _rev_parse(head, _run)
        # The base this head was tested on top of. kirocrew-prepare-pr rebases before
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
        # Only an overlap leads to a rebase, so only an overlap needs the budget
        # read -- a FRESH answer pays for no extra API call. Without a PR number
        # there is nowhere to read the count from, and the cap cannot apply.
        resyncs = _resync_count(pr, _run) if (overlap and pr) else -1
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
                "resyncs": resyncs,
                "cap_spent": resyncs >= RESYNC_CAP,
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
    line = "green age: base +{} commits ({} -> {}), overlap: {}".format(
        summary.get("commits", 0),
        (summary.get("ci_base") or "")[:8],
        (summary.get("base_head") or "")[:8],
        what,
    )
    # Printed only once a budget has actually been read, so the common FRESH line
    # is unchanged. A poll cycle reading this sees how close the PR is to being
    # handed over, rather than finding out only at the cap.
    resyncs = summary.get("resyncs", -1)
    if resyncs >= 0:
        line += ", re-syncs: {}/{}".format(resyncs, RESYNC_CAP)
    return line


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
        if summary["stale"] and summary["cap_spent"]:
            # The overlap is real, so the green IS stale -- but another rebase is
            # the wrong answer to it. Said as its own code because the two
            # verdicts call for opposite actions and a caller must not have to
            # read prose to tell them apart.
            code = EXIT_CAP_SPENT
            err(
                "CAP SPENT: this PR has already been re-synced {} time(s), the limit "
                "is {}, and origin/{} has moved again in {} file(s) it touches. Do NOT "
                "rebase: each one discards a green the PR already earned. Report which "
                "files the base keeps moving in and hand the PR to a person.".format(
                    summary["resyncs"], RESYNC_CAP, args.base, len(summary["overlap"])
                )
            )
        elif summary["stale"]:
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

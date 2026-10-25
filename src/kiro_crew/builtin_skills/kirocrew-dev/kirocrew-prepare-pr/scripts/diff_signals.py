#!/usr/bin/env python3
"""diff_signals.py - change-signal inventory for the description-reconcile step.

Lists changed files by status vs the base branch and flags notable structural
signals so the PR body can be checked for completeness against what the diff
actually does. Stdlib only; portable across OSes.

With ``--check-body`` it also reads the PR body the skill writes --
``<git-dir>/prepare-pr-body.md``, with ``<git-dir>`` from
``git rev-parse --absolute-git-dir`` -- and runs four checks on it:

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
* **Sibling sweep (advisory until CI enforces it).** On a ``fix``/``revert`` title (the
  branch's first commit subject) the body's ``## Sibling sweep`` section is judged by
  ``.github/scripts/sibling_list_check.py``, also read from ``origin/<base>``. While CI's
  step is in warn mode the findings print and the exit stays 0; once the script turns red
  for this pull request, the same findings are exit 23. A base without the script skips it.

* **Proof line (advisory until CI enforces it).** On a ``fix`` title (the branch's first
  commit subject) the body's ``Proof:`` line must sit inside ``## Tests``.
  ``.github/scripts/proof_line_check.py``, also read from ``origin/<base>``, judges it, and
  compares it with ``<git-dir>/prepare-pr-prove.json`` -- the record ``prove.py`` writes --
  for the same tree, so a stale or invented line fails here though CI cannot see the
  record. While CI's step is in warn mode the findings print and the exit stays 0; once the
  script turns red for this pull request the same findings are exit 24. A base without the
  script skips it.

* **Gate proof (advisory).** A ``Gate proof:`` line in the body is a claim that
  ``pre_push_gate.py`` ran. It is checked against ``<git-dir>/prepare-pr-gate-proof.json``
  for HEAD's tree; a line the proof contradicts, or that has no proof behind it, is exit
  25. A body with no such line never fails: the proof is optional.

* **Pattern harvest.** On a ``fix``/``revert`` title the body needs a
  ``## Pattern harvest`` heading and a ``Rule candidate:`` or ``Not generalizable:``
  line, the rule CI's PR Hygiene step enforces inline in ``code-review.yml`` (so there is
  no script to run from the base). A miss prints CI's message and is exit 26. The
  three ``HARVEST_*`` patterns are that step's; a test fails if the workflow's change.

All checks run and print before any exit; when several breach, the lowest code wins.

Size and age signals are printed, never an exit code. ``! SIZE`` appears at
``SIZE_HINT_FILES`` changed files and says more at ``SIZE_STOP_FILES``, where CI's
``PR Size`` check (``.github/scripts/pr_size.py``) wants an owner's approval once it
is enforced. ``--age`` adds ``! AGE`` from ``gh pr view`` when the pull request has
been open ``AGE_HOURS`` hours; it is opt-in because it is the one call that leaves
the machine.

Usage:  python3 diff_signals.py [base-branch] [--check-body] [--age]
Exit:   0 printed / body passes | 20 unaccounted area(s) | 21 What changed too long |
        22 template sections missing | 23 Sibling sweep missing or malformed (when enforced) |
        24 Proof line missing, malformed or stale (when enforced) |
        25 a Gate proof: line the recorded proof contradicts |
        26 Pattern harvest heading or line missing on a fix/revert |
        2 environment error
"""

import argparse
import importlib.machinery
import importlib.util
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

# CI's Sibling sweep check, read from origin/<base> by --check-body.
SIBLING_CHECK = ".github/scripts/sibling_list_check.py"

# CI's Proof line check, read from origin/<base> by --check-body, and the record
# prove.py writes for it to compare against (inside the git directory).
PROOF_CHECK = ".github/scripts/proof_line_check.py"
PROVE_RECORD = "prepare-pr-prove.json"

# CI's Pattern harvest rule, copied from PR Hygiene in .github/workflows/code-review.yml
# (grep -qiE there, re.IGNORECASE | re.MULTILINE here). A test pins each to the workflow.
HARVEST_TITLE_RE = r"^(fix|revert)([(!:]|$)"
HARVEST_HEADING_RE = r"^##+ *Pattern harvest"
HARVEST_LINE_RE = r"^ *(Rule candidate|Not generalizable) *:"

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


class _NoBytecodeSourceLoader(importlib.machinery.SourceFileLoader):
    """Load shipped source normally while suppressing cache writes."""

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def set_data(self, path, data, *, _mode=0o666):
        return None


_CONTRACT: dict = {}


def _review_contract():
    """The shared contract module beside this script (the one fence reader), loaded once."""
    if "module" not in _CONTRACT:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_review_contract.py")
        loader = _NoBytecodeSourceLoader("_prepare_pr_diff_signals_contract", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        if spec is None:  # pragma: no cover - defensive
            raise RuntimeError("cannot import kirocrew-prepare-pr sibling: " + path)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _CONTRACT["module"] = module
    return _CONTRACT["module"]


def _what_changed_unfenced(body):
    """The ``## What changed`` section with fenced blocks removed, or None if absent."""
    m = re.search(r"^## +What changed[^\n]*\n(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    if not m:
        return None
    return _review_contract().drop_fenced_blocks(m.group(1))


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


def gate_proof_check(body, gitdir):
    """Return 25 when a ``Gate proof:`` line in ``body`` contradicts the recorded proof, else 0.

    ``pre_push_gate.py`` owns the proof format, so it is loaded from beside this script.
    Without a ``Gate proof:`` line, or without that script, there is nothing to contradict.
    """
    if "Gate proof:" not in body:
        return 0
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pre_push_gate.py")
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ here
    try:
        spec = importlib.util.spec_from_file_location("pre_push_gate", path)
        if spec is None or spec.loader is None:
            raise ImportError(path)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
    except (AttributeError, ImportError, OSError, SyntaxError):
        return 0
    finally:
        sys.dont_write_bytecode = previous
    tree = run(["git", "rev-parse", "HEAD^{tree}"])[1].strip()
    problem = gate.contradiction(body, gitdir, tree)
    if problem:
        print("GATE PROOF: {} - fix the line or delete it; the proof is optional".format(problem))
        return 25
    return 0


def harvest_problem(title, body):
    """CI's Pattern harvest message for ``title``/``body``, or None when it passes."""
    flags = re.IGNORECASE | re.MULTILINE
    if not re.search(HARVEST_TITLE_RE, title, re.IGNORECASE):
        return None
    if not re.search(HARVEST_HEADING_RE, body, flags):
        return "Fix/revert PRs must carry a '## Pattern harvest' section."
    if not re.search(HARVEST_LINE_RE, body, flags):
        return "The Pattern harvest section must contain a 'Rule candidate:' or 'Not generalizable:' line."
    return None


def harvest_check(body, base):
    """Apply CI's Pattern harvest rule to ``body``; return 0 or 26."""
    # The same title the Sibling check reads: the branch's first commit subject.
    subjects = run(["git", "log", "--reverse", "--format=%s", "origin/{}..HEAD".format(base)])[1]
    title = subjects.splitlines()[0].strip() if subjects.strip() else ""
    problem = harvest_problem(title, body)
    if problem:
        print("PATTERN HARVEST: " + problem)
        return 26
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


def sibling_check(body, base):
    """Run the base ref's ``SIBLING_CHECK`` on ``body``; return 0 or 23.

    The script prints its findings and exits 1 only when it is enforcing, so a
    warn-mode finding is shown here and never stops the loop. Absent on the base
    (another repo, or before the check landed) is a silent skip.
    """
    rc, script, _ = run(["git", "show", "origin/{}:{}".format(base, SIBLING_CHECK)])
    if rc != 0:
        return 0
    # The pull request title is the squash message of a one-commit branch; with the
    # permitted second (mechanical) commit it is still the first commit's subject.
    subjects = run(["git", "log", "--reverse", "--format=%s", "origin/{}..HEAD".format(base)])[1]
    title = subjects.splitlines()[0].strip() if subjects.strip() else ""
    merge_base = run(["git", "merge-base", "origin/" + base, "HEAD"])[1].strip()
    head = run(["git", "rev-parse", "HEAD"])[1].strip()
    if not (title and merge_base and head):
        print("WARN: cannot resolve HEAD or the merge-base - sibling sweep check skipped")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "sibling_list_check.py")
        with open(script_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        env = dict(os.environ, PR_TITLE=title, PR_BODY=body, MERGE_BASE=merge_base, HEAD_SHA=head)
        env.pop("GITHUB_ACTIONS", None)  # plain lines here, not workflow annotations
        env.pop("PR_CREATED_AT", None)
        try:
            p = subprocess.run(
                [sys.executable, script_path],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            print(
                "WARN: cannot run {} ({}) - sibling sweep check skipped".format(SIBLING_CHECK, exc)
            )
            return 0
    for line in p.stdout.splitlines():
        print(line)
    if p.returncode == 1:
        return 23
    if p.returncode != 0:
        print("WARN: {} could not run ({}) - skipped".format(SIBLING_CHECK, p.stderr.strip()))
    return 0


def proof_check(body, base, gitdir):
    """Run the base ref's ``PROOF_CHECK`` on ``body``; return 0 or 24.

    The script prints its findings and exits 1 only when it is enforcing, so a
    warn-mode finding is shown here and never stops the loop. Absent on the base
    (another repo, or before the check landed) is a silent skip.
    """
    rc, script, _ = run(["git", "show", "origin/{}:{}".format(base, PROOF_CHECK)])
    if rc != 0:
        return 0
    # The pull request title is the squash message of a one-commit branch; with the
    # permitted second (mechanical) commit it is still the first commit's subject.
    subjects = run(["git", "log", "--reverse", "--format=%s", "origin/{}..HEAD".format(base)])[1]
    title = subjects.splitlines()[0].strip() if subjects.strip() else ""
    tree = run(["git", "rev-parse", "HEAD^{tree}"])[1].strip()
    if not (title and tree):
        print("WARN: cannot resolve HEAD - proof line check skipped")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        script_path = os.path.join(tmp, "proof_line_check.py")
        with open(script_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        env = dict(
            os.environ,
            PR_TITLE=title,
            PR_BODY=body,
            TREE_ID=tree,
            PROVE_RECORD=os.path.join(gitdir, PROVE_RECORD),
        )
        env.pop("GITHUB_ACTIONS", None)  # plain lines here, not workflow annotations
        env.pop("PR_CREATED_AT", None)
        try:
            p = subprocess.run(
                [sys.executable, script_path],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            print("WARN: cannot run {} ({}) - proof line check skipped".format(PROOF_CHECK, exc))
            return 0
    for line in p.stdout.splitlines():
        print(line)
    if p.returncode == 1:
        return 24
    if p.returncode != 0:
        print("WARN: {} could not run ({}) - skipped".format(PROOF_CHECK, p.stderr.strip()))
    return 0


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

    body = gitdir = None
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
        # Evaluated before the return: every check prints, then the lowest code wins.
        sibling = sibling_check(body, base)
        proof = proof_check(body, base, gitdir)
        gate_proof = gate_proof_check(body, gitdir)
        harvest = harvest_check(body, base)
        return rc or tpl or sibling or proof or gate_proof or harvest
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

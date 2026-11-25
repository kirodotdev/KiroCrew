#!/usr/bin/env python3
"""Docs-only scope: is a pull request's diff nothing but prose documentation?

A PR that changes only prose docs cannot change what the builds or CodeQL
judge, and only some tests read docs, so CI skips its matrix, the builds and
CodeQL for it and PR Readiness counts CodeQL as not applicable. The Opus and
GPT review lanes still run: they enforce blocking review rules that judge
prose. Every consumer asks this one
script, so the CI side that skips a lane and the readiness side that excuses it
cannot disagree about which PRs qualify:

* ``ci.yml`` (the ``changes`` job), ``build.yml`` and ``codeql.yml`` skip
  their heavy jobs on ``true``;
* ``pr-readiness.yml`` reads its own default-branch copy, so a PR cannot
  change the rule it is judged by, and excuses the lanes that skip.

Prose docs are exactly these paths:

    docs/**/*.md
    src/kiro_crew/docs/**/*.md
    website/docs/**/*.md
    README.md            (repository root only)
    CONTRIBUTING.md      (repository root only)

Anything else, including a script or an asset under ``docs/``, means the diff
is NOT docs-only. A rename counts both its old and its new path, so moving code
into ``docs/`` is not docs-only either.

Usage:
    docs_only.py classify [--expect-files N] < input
        stdin is either newline-separated paths (``git diff --name-only
        --no-renames``) or, with ``--expect-files``, the JSON body of the
        ``compare`` REST endpoint. ``--expect-files`` is the PR's
        ``changed_files``: a compare listing of any other length (truncated,
        or a head that moved) is not trusted. Prints ``docs_only=true`` or
        ``docs_only=false`` for $GITHUB_OUTPUT and the reason on stderr.
        Every input it cannot read prints ``false``: the failure direction is
        "run everything", never a skip.
    docs_only.py readers PATH...
        Prints the test files that could read one of the changed docs, one per
        line: backend ``test_*.py`` files under every ``[tool:pytest]
        testpaths`` root of ``setup.cfg`` (``test`` when that cannot be read)
        and frontend ``website/src/**`` ``*.test.ts(x)`` files that name a
        changed doc's file name, carry any ``.md`` string literal (``"*.md"``,
        ``suffix == ".md"``, ``endswith(".md")``, ``"README.md"``) or name
        ``source_corpus``, plus test files importing a helper module under
        those roots that does any of these.
        Electron specs are not selected because Docs Contract Tests runs the
        whole Electron suite.
        Best-effort and over-broad in the safe direction; main's push CI still
        runs every suite.

Exit 0 = answer printed; 2 = usage error.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import sys

# The compare endpoint lists at most 300 files; a larger PR is never excused.
MAX_FILES = 250

_DOC_TREES = ("docs/", "src/kiro_crew/docs/", "website/docs/")
_ROOT_DOCS = frozenset({"README.md", "CONTRIBUTING.md"})

# A test can read any doc without naming it: by globbing or filtering on the
# ``.md`` suffix, or by walking the repository through ``source_corpus``. Any
# ``.md`` string literal counts, which over-selects on purpose.
_MD_LITERAL = re.compile(r"""\.md['"]""")
_CORPUS = "source_corpus"
_TEST_DIRS = frozenset({"test", "tests"})


def is_prose_doc(path: str) -> bool:
    """True when ``path`` (repository-relative, ``/``-separated) is a prose doc."""
    if path in _ROOT_DOCS:
        return True
    return path.endswith(".md") and path.startswith(_DOC_TREES)


def classify(paths: list[str]) -> tuple[bool, str]:
    """Decide docs-only for a list of changed paths; returns (answer, reason)."""
    if not paths:
        return False, "no changed paths"
    if len(paths) > 2 * MAX_FILES:
        return False, f"{len(paths)} paths is more than this check reads"
    other = sorted({p for p in paths if not is_prose_doc(p)})
    if other:
        shown = ", ".join(other[:5]) + (" ..." if len(other) > 5 else "")
        return False, f"{len(other)} non-doc path(s): {shown}"
    return True, f"all {len(set(paths))} changed path(s) are prose docs"


def paths_from_compare(body: str, expect_files: int) -> tuple[list[str], str]:
    """Paths of a ``compare`` response, or ([], reason) when it cannot be trusted."""
    if expect_files < 1 or expect_files > MAX_FILES:
        return [], f"changed_files={expect_files} is outside 1..{MAX_FILES}"
    try:
        files = json.loads(body).get("files")
    except (ValueError, AttributeError):
        return [], "compare response is not JSON"
    if not isinstance(files, list) or len(files) != expect_files:
        got = len(files) if isinstance(files, list) else "no"
        return [], f"compare listed {got} file(s), the PR has {expect_files}"
    paths = []
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            return [], "compare entry without a filename"
        paths.append(entry["filename"])
        if isinstance(entry.get("previous_filename"), str):
            paths.append(entry["previous_filename"])
    return paths, ""


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _walk(root: str, top: str, keep) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, top)):
        dirnames[:] = [d for d in dirnames if d not in {"node_modules", "__pycache__"}]
        for name in filenames:
            if keep(name):
                found.append(
                    os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
                )
    return sorted(found)


def _backend_roots(root: str) -> list[str]:
    """The pytest ``testpaths`` roots from ``setup.cfg``, or ``["test"]``."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(os.path.join(root, "setup.cfg"), encoding="utf-8")
        value = parser.get("tool:pytest", "testpaths", fallback="")
    except (configparser.Error, UnicodeDecodeError):
        value = ""
    roots = [p.strip("/") for p in value.split() if p.strip("/")]
    return roots or ["test"]


def _read(root: str, rel: str) -> str:
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def readers(changed: list[str], root: str | None = None) -> list[str]:
    """Test files that could read one of ``changed`` docs (see the module doc)."""
    root = root or _repo_root()
    names = sorted({os.path.basename(p) for p in changed if p})
    if not names:
        return []

    def mentions(text: str) -> bool:
        if _MD_LITERAL.search(text) or _CORPUS in text:
            return True
        return any(n in text for n in names)

    backend = sorted(
        {
            rel
            for top in _backend_roots(root)
            for rel in _walk(root, top, lambda n: n.endswith(".py"))
        }
    )
    texts = {rel: _read(root, rel) for rel in backend}
    picked = set()
    helpers = []
    for rel, text in texts.items():
        if not mentions(text):
            continue
        if os.path.basename(rel).startswith("test_"):
            picked.add(rel)
        elif _TEST_DIRS.intersection(rel.split("/")[:-1]):
            # Only modules inside a test directory are helpers; the
            # application code beside an app's tests is not.
            helpers.append(os.path.splitext(os.path.basename(rel))[0])
    # One level of indirection: a helper module that reads a doc is reached
    # through the tests that import it: by bare name, relative name, or a
    # dotted path through a ``test``/``tests`` package.
    if helpers:
        imports = re.compile(
            r"\b(?:import|from)\s+(?:\.+|(?:[\w.]*\.)?tests?\.)?(?:%s)\b"
            % "|".join(map(re.escape, helpers))
        )
        for rel, text in texts.items():
            if os.path.basename(rel).startswith("test_") and imports.search(text):
                picked.add(rel)

    frontend = _walk(root, "website/src", lambda n: n.endswith((".test.ts", ".test.tsx")))
    picked.update(rel for rel in frontend if mentions(_read(root, rel)))
    return sorted(picked)


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "readers":
        for rel in readers(argv[2:]):
            print(rel)
        return 0
    if len(argv) < 2 or argv[1] != "classify":
        print(__doc__, file=sys.stderr)
        return 2
    expect = None
    rest = argv[2:]
    if rest:
        if len(rest) != 2 or rest[0] != "--expect-files":
            print(__doc__, file=sys.stderr)
            return 2
        try:
            expect = int(rest[1])
        except ValueError:
            expect = -1
    body = sys.stdin.read()
    if expect is None:
        paths = [line.strip() for line in body.splitlines() if line.strip()]
        reason = ""
    else:
        paths, reason = paths_from_compare(body, expect)
    if reason:
        answer = False
    else:
        answer, reason = classify(paths)
    print(f"docs_only={'true' if answer else 'false'}")
    print(f"docs-only scope: {'true' if answer else 'false'} ({reason})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

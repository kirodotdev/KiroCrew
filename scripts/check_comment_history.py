#!/usr/bin/env python3
"""check_comment_history.py -- no change history narrated in comments or docstrings.

## The rule this enforces

``docs/system-specs/common/code-style.md`` ("Comments explain the WHY") says a
comment must not carry PR or review numbers, ticket ids, commit SHAs, incident
dates, or historical narration -- "previously", "used to", "we now",
"Status: implemented". That history lives in git, and a comment that narrates a
change is stale the moment the next change lands: a reader cannot tell whether it
describes the code in front of them or the code it replaced.

## What counts as a violation

A COMMENT token or a DOCSTRING whose text matches one of ``PATTERNS``. Both are
found with ``tokenize`` and ``ast``, so a string literal that is not a docstring
is never scanned -- a user-facing error message reading "this token is no longer
valid" is behavior, not narration, and flagging it would make the gate wrong in
the one place it is most tempting to write the words.

One violation is one distinct MATCHED SPAN, not one line. Overlapping hits
collapse to the widest one, so a single ``(#4211)`` that two patterns both
recognise is reported once.

Pragma comments (``# type: ignore``, ``# noqa``, ``# pragma``, ``# fmt:``) are
exempt, as code-style.md says, and ``src/kiro_crew/_vendor/`` is excluded because
vendored third-party code is not ours to rewrite.

## Diff-scoped, like the brand-name gate

The gate judges only the lines THIS change adds, measured against the base ref in
``COMMENT_HISTORY_BASE_REF`` (inside Actions, the PR's ``base.sha``). Added lines
are complete for regression -- a line only reaches main through a diff that added
it -- and they are the only lines a contributor can act on: CI evaluates a PR's
merge ref, so a whole-tree or per-file-count verdict would redden a PR because
the base branch merged someone else's file.

There is deliberately NO baseline file. A per-file count ratchet needs one shared
JSON that every cleanup PR must lower in the same change, which makes that file a
merge-conflict hotspot for as long as any cleanup is in flight anywhere in the
tree. Judging added lines needs no shared state at all. Existing markers are
therefore not tracked; a marker can re-enter only on an added line, which is
exactly what the gate judges.

Without ``COMMENT_HISTORY_BASE_REF`` the script reports whole-tree counts and
does not enforce, so a local run is informative rather than red on a tree that
still carries the legacy set.

    COMMENT_HISTORY_BASE_REF=origin/main python3 scripts/check_comment_history.py
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import os
import re
import subprocess
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = "COMMENT_HISTORY_BASE_REF"
DEFAULT_TARGETS = ("src/kiro_crew", "test")
# Vendored third-party code is not ours to rewrite, and code-style.md exempts it.
EXCLUDED_DIRS = ("src/kiro_crew/_vendor/",)
# code-style.md exempts pragmas: they are tool directives, not prose.
PRAGMA_PREFIXES = ("type:", "noqa", "pragma", "fmt:")

#: Each pattern names one way a comment records history instead of behavior.
PATTERNS: tuple[re.Pattern[str], ...] = (
    # A parenthesised issue reference -- the canonical "(#1234)" changelog tail.
    re.compile(r"\(#\d{3,5}\)"),
    # "issue 1234" / "issue #1234": the ticket, not what the code does.
    re.compile(r"\bissues?\s+#?\d{3,5}\b", re.IGNORECASE),
    # "PR #1234" / "PR 1234": which pull request, which is git's to remember.
    re.compile(r"\bPRs?\s+#?\d{3,5}\b"),
    # A bare "#1234" issue reference. Four digits minimum and a following word
    # boundary, so a hex colour (#ffffff) and a hash-prefixed hex byte cannot
    # match: the class is digits-only.
    re.compile(r"(?<![\w#])#\d{4,5}\b(?!\.\d)"),
    # "regression for/from X" -- names the incident the code answers. A bare
    # "regression test pins this" is present-tense purpose, so it is NOT matched.
    re.compile(r"\bregressions?\s+(?:for|from)\b", re.IGNORECASE),
    # An incident or milestone date. A comment dated to a day is a log entry.
    re.compile(r"\b20\d\d-\d\d-\d\d\b"),
    # Historical narration: what the code used to do.
    re.compile(r"\bpreviously\b", re.IGNORECASE),
    re.compile(r"\bused to\b", re.IGNORECASE),
    re.compile(r"\bno longer\b", re.IGNORECASE),
    re.compile(r"\bhistorically\b", re.IGNORECASE),
    # "we now X" -- present behavior stated as a change away from something.
    re.compile(r"\bwe now\b", re.IGNORECASE),
    # Review-round and finding markers: the review's bookkeeping, not the code's.
    re.compile(r"\bGPT round\b", re.IGNORECASE),
    re.compile(r"\breview round\b", re.IGNORECASE),
    re.compile(r"\bround \d+\b", re.IGNORECASE),
    # A task-log status line. The code IS the status.
    re.compile(r"Status:\s*implemented", re.IGNORECASE),
    # "hotfix" / "follow-up to" -- the change's place in a sequence of changes.
    re.compile(r"\bhotfix(?:e[sd])?\b", re.IGNORECASE),
    re.compile(r"\bfollow-ups? to\b", re.IGNORECASE),
    # A commit SHA. Only after the literal word "commit", because a bare 7-40
    # char hex run also spells a hash, an id and half the English lowercase
    # words made of abcdef.
    re.compile(r"\bcommit\s+[0-9a-f]{7,40}\b"),
)

#: Union of every pattern above, case-insensitive: a source text this cannot
#: match anywhere has no violation in any comment or docstring either, so the
#: file skips tokenize and ast entirely. Derived from PATTERNS rather than spelled
#: out, so a pattern added above cannot be missing from the pre-filter -- a
#: hand-written copy that fell behind would silently hide the new rule's hits.
_ANY_MARKER = re.compile("|".join(f"(?:{pattern.pattern})" for pattern in PATTERNS), re.IGNORECASE)


def _load_scope():
    """The shared diff-scope helpers (see scripts/ratchet_scope.py).

    Loaded by path, not imported: ``scripts/`` is not a package, so a plain
    import would resolve only by accident of ``sys.path[0]`` -- and not at all
    when a test loads this gate by path. The pair lives there because the
    merge-ref ratchets need identical answers; a private copy per gate is how
    they would come to disagree about the same added line.
    """
    script = ROOT / "scripts" / "ratchet_scope.py"
    spec = importlib.util.spec_from_file_location("ratchet_scope", script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pragma(comment_body: str) -> bool:
    """True for a tool directive rather than prose (code-style.md exempts them)."""
    stripped = comment_body.lstrip("#").strip()
    lowered = stripped.lower()
    return any(lowered.startswith(prefix) for prefix in PRAGMA_PREFIXES)


def _matches(text: str) -> list[tuple[int, int, str]]:
    """(start offset, end offset, matched text) for each distinct hit, overlaps collapsed.

    Several patterns describe the same marker on purpose -- a parenthesised issue
    reference is also a bare issue number -- so counting raw hits would report one
    reference as two. A reader who deletes it would then watch the count fall by
    two and reasonably conclude the gate is wrong.

    Both OFFSETS are returned because a docstring hit has to be judged on the
    physical lines it actually covers, not the docstring's first line: the
    added-line rule compares against the lines a diff touched, and a pattern such
    as ``issue\\s+#1234`` can start on an old line and end on an added one.
    """
    spans: list[tuple[int, int, str]] = []
    for pattern in PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), match.group(0)))
    # Widest span first at each start, so a contained hit is always the one dropped.
    spans.sort(key=lambda span: (span[0], -span[1]))
    kept: list[tuple[int, int, str]] = []
    for start, end, matched in spans:
        if any(start >= k_start and end <= k_end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, matched))
    return [(start, end, matched) for start, end, matched in kept]


def _docstring_nodes(tree: ast.AST) -> list[ast.Constant]:
    """The docstring literal of every module, class and function in ``tree``.

    Only these. Walking the tree and asking each docstring-bearing node for its
    first statement is what keeps an ordinary string literal -- a user-facing
    message that legitimately contains "no longer" -- out of scope. A gate that
    scanned all strings would flag behavior as narration.
    """
    nodes: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            nodes.append(first.value)
    return nodes


def violation_spans(source: str) -> list[tuple[int, int, str]]:
    """(first line, last line, matched text) for every history marker.

    A marker is reported over every physical line its match covers. Comments are
    single-line tokens, so first == last there; a docstring match can cross a line
    break (``issue`` at the end of one line, ``#1234`` starting the next), and the
    added-line rule must see BOTH lines or an appended continuation slips past.

    Raises ``SyntaxError`` when the source does not parse, and
    ``tokenize.TokenError`` when it does not tokenize: both are hard errors for
    the caller, never "clean": a parse failure read as zero violations would
    let a file that no longer parses pass the gate.
    """
    found: list[tuple[int, int, str]] = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT or _is_pragma(token.string):
            continue
        for _, _, text in _matches(token.string):
            found.append((token.start[0], token.start[0], text))

    for node in _docstring_nodes(ast.parse(source)):
        for start, end, text in _matches(node.value):
            # The lines the marker is ON, not the docstring's first line. Reporting
            # the opening line would put every hit in a multi-line docstring
            # outside the set of lines the diff added, so the added-line rule --
            # the one that catches swapping a fresh marker in for an old one --
            # would never fire inside a docstring.
            #
            # Counted over the PARSED value, so an escaped \n in a single-line
            # docstring shifts the report down a line. Docstrings spell newlines
            # literally, so this is the rare case, and the answer still lands
            # inside the literal being fixed. ``end - 1``: the end offset is
            # exclusive, so a match ending exactly at a newline stays on its line.
            first = node.lineno + node.value.count("\n", 0, start)
            last = node.lineno + node.value.count("\n", 0, max(start, end - 1))
            found.append((first, last, text))

    return sorted(found)


def violations_in_source(source: str) -> list[tuple[int, str]]:
    """(line, matched text) for every history marker: the report shape.

    The line is the one the marker STARTS on. Judging which lines a marker
    touches is :func:`violation_spans`' job; this is what gets printed.
    """
    return [(first, text) for first, _, text in violation_spans(source)]


def _excluded(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXCLUDED_DIRS)


def _in_targets(rel: str) -> bool:
    return rel.endswith(".py") and rel.startswith(DEFAULT_TARGETS) and not _excluded(rel)


def _violations_for(rel: str) -> list[tuple[int, int, str]]:
    """Every marker span in ``rel`` (working tree), or a hard error if it does not parse."""
    source = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
    if not _ANY_MARKER.search(source):
        return []
    try:
        return violation_spans(source)
    except (SyntaxError, tokenize.TokenError) as exc:
        raise SystemExit(
            f"{rel} does not parse ({exc}); refusing to read a parse failure as zero " "violations"
        )


def _scan_tree() -> dict[str, list[tuple[int, int, str]]]:
    """Whole-tree map of repo-relative path -> violations, for the report mode."""
    results: dict[str, list[tuple[int, int, str]]] = {}
    for name in DEFAULT_TARGETS:
        target = ROOT / name
        if not target.is_dir():
            raise SystemExit(f"target {name} does not exist under {ROOT}")
        for path in sorted(target.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if _excluded(rel):
                continue
            found = _violations_for(rel)
            if found:
                results[rel] = found
    return results


def added_line_violations(
    violations: dict[str, list[tuple[int, int, str]]],
    added: dict[str, set[int]],
) -> dict[str, list[tuple[int, str]]]:
    """The markers that touch a line this change ADDED, per file.

    A marker on a pre-existing line is not judged: it is the base branch's, and
    the contributor in front of this gate did not write it. A marker on an added
    line is the contributor's, whether the line is brand new or a rewrite of an
    old one -- so swapping a fresh marker in for an old one is caught, at any
    count.

    Judged over EVERY line the match spans, not just its first. A docstring
    marker such as ``issue #1234`` can have ``issue`` on an old line and
    ``#1234`` on an added continuation; attributing it to the start line alone
    would let the appended half through. Reported at the start line.
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    for rel, found in sorted(violations.items()):
        added_here = added.get(rel, set())
        on_added = [
            (first, text)
            for first, last, text in found
            if any(line in added_here for line in range(first, last + 1))
        ]
        if on_added:
            offenders[rel] = on_added
    return offenders


def _report(rel: str, found: list[tuple[int, str]]) -> None:
    for line, text in found:
        print(f"  {rel}:{line}: {text}")


def enforce_diff(base: str) -> int:
    """Enforce on the lines this change adds. Fails closed when the base is unreadable."""
    scope = _load_scope()
    frm = scope.resolve_base(base)
    try:
        changed = [p for p in scope.changed_paths_at(frm) if _in_targets(p)]
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"::error::comment-history gate: cannot diff against {frm} -- the base "
            f"commit is not present. Fetch it before running, or unset {BASE_ENV} to "
            f"report whole-tree counts without enforcing.\n{exc.stderr}"
        )
    print(f"comment-history gate scope: {base}..working tree ({len(changed)} changed file(s))")

    violations: dict[str, list[tuple[int, int, str]]] = {}
    added: dict[str, set[int]] = {}
    for rel in changed:
        if not (ROOT / rel).is_file():
            continue  # a deletion adds no lines
        lines = scope.added_lines_at(frm, rel)
        if not lines:
            continue
        found = _violations_for(rel)
        if found:
            violations[rel] = found
            added[rel] = lines

    offenders = added_line_violations(violations, added)
    for rel, found in offenders.items():
        print(
            f"::error file={rel}::this change ADDS history narration to a comment or "
            "docstring (PR/issue number, review round, commit SHA, or a phrase like "
            '"previously" / "no longer" / "we now"). State CURRENT behavior in present '
            "tense; the history is in git. See docs/system-specs/common/code-style.md."
        )
        _report(rel, found)
    if offenders:
        total = sum(len(found) for found in offenders.values())
        print(
            f"\ncomment-history gate FAILED: {total} marker(s) on added lines in "
            f"{len(offenders)} file(s)."
        )
        return 1
    print("comment-history gate passed: no history narration on the lines this change adds.")
    return 0


def report_tree() -> int:
    """Whole-tree counts, never failing: the number to watch, not a gate."""
    violations = _scan_tree()
    total = sum(len(found) for found in violations.values())
    print(
        f"comment-history report: {total} marker(s) in {len(violations)} file(s) across "
        f"{', '.join(DEFAULT_TARGETS)}. Not enforced here; set {BASE_ENV} to gate a change."
    )
    return 0


def _self_test() -> int:
    """Plant one probe per rule family; a broken rule fails here, not in prod."""
    flagged_probes = {
        "parenthesised issue ref": "x = 1  # widen the timeout (#4211)\n",
        "issue word form": "y = 2  # guards issue 4211\n",
        "PR word form": "z = 3  # see PR #812\n",
        "bare issue number": "a = 4  # tracked as #4211\n",
        "regression for": "b = 5  # regression for the truncated parse\n",
        "incident date": "b2 = 5  # the queue drained wrong on 2026-04-11\n",
        "previously": "c = 6  # previously this parsed lazily\n",
        "used to": "d = 7  # this used to accept bytes\n",
        "no longer": "e = 8  # the cache is no longer consulted\n",
        "historically": "f = 9  # historically the loop was sync\n",
        "we now": "g = 10  # we now resolve the path first\n",
        "GPT round": "h = 11  # GPT round 2 asked for this\n",
        "review round": "i = 12  # review round three finding\n",
        "round N": "j = 13  # round 4 rework\n",
        "status line": "k = 14  # Status: implemented\n",
        "hotfix": "m = 15  # hotfix for the launch\n",
        "follow-up to": "n = 16  # follow-up to the sandbox change\n",
        "commit SHA": "o = 17  # see commit 4a0de1d\n",
        "module docstring": '"""Parse the manifest. Previously it read YAML."""\n',
        "function docstring": (
            "def f():\n" '    """Return the path. We now resolve symlinks."""\n' "    return 1\n"
        ),
        "class docstring": (
            "class C:\n" '    """Holds the state. Hotfix for the leak."""\n' "    x = 1\n"
        ),
    }
    clean_probes = {
        "plain WHY comment": "x = 1  # the child writes CRLF, so newlines are normalized\n",
        "non-docstring string literal": 'MESSAGE = "this token is no longer valid"\n',
        "string literal after a docstring": (
            '"""Module."""\n' 'HINT = "the flag was previously named --slow"\n'
        ),
        "type pragma": "x: int = 1  # type: ignore[assignment]\n",
        "noqa pragma": "import os  # noqa: F401\n",
        "coverage pragma": "if False:  # pragma: no cover\n    pass\n",
        "fmt pragma": "x = [1]  # fmt: off\n",
        "hex colour": 'COLOUR = "#ffffff"  # the dashboard accent, hex #ffffff\n',
        "short hex is not a SHA": "x = 1  # the accent is beef\n",
        "bare hex without the word commit": "x = 1  # digest 4a0de1d identifies the blob\n",
        "three-digit bare number": "x = 1  # HTTP #404 is not an issue ref\n",
        "version number": "x = 1  # requires Python 3.12\n",
        "round without a number": "x = 1  # round the interval up to the next second\n",
        "regression as present-tense purpose": "x = 1  # regression test pins this shape\n",
        "a version-like number is not a date": "x = 1  # the wire form is 3.12-0\n",
    }
    failures: list[str] = []
    for label, source in flagged_probes.items():
        if not violations_in_source(source):
            failures.append(f"NOT flagged but should be: {label}")
    for label, source in clean_probes.items():
        if violations_in_source(source):
            failures.append(f"flagged but should be clean: {label}")
    # Two distinct markers report twice; the same marker matched by two patterns
    # reports once.
    if len(violations_in_source("p = 1  # previously, see PR #812\n")) != 2:
        failures.append("two distinct markers in one comment must count 2")
    if len(violations_in_source("q = 1  # widen the timeout (#4211)\n")) != 1:
        failures.append("one marker matched by two patterns must count 1")
    # A marker deep in a docstring must report ITS line, or the added-line rule
    # cannot see it.
    interior = '"""Head.\n\nTail: previously it blocked.\n"""\n'
    if violations_in_source(interior) != [(3, "previously")]:
        failures.append("a docstring marker must report the line it sits on")
    # A marker wrapped across a line break inside a docstring spans BOTH lines:
    # the added-line rule must see the continuation, or ``issue`` on an old line
    # plus ``#1234`` appended on a new one would pass.
    wrapped = '"""Head.\n\nSee issue\n#4211 for the shape.\n"""\n'
    if violation_spans(wrapped) != [(3, 4, "issue\n#4211")]:
        failures.append(
            f"a wrapped docstring marker must span both lines: {violation_spans(wrapped)}"
        )
    for failure in failures:
        print(f"::error::self-test: {failure}")
    if failures:
        return 1
    print(
        f"self-test passed: {len(flagged_probes)} flagged probes, "
        f"{len(clean_probes)} clean probes."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--test",
        action="store_true",
        help="run the rule-family self-test instead of the gate",
    )
    args = parser.parse_args(argv)
    if args.test:
        return _self_test()
    base = os.environ.get(BASE_ENV, "").strip()
    if base:
        return enforce_diff(base)
    return report_tree()


if __name__ == "__main__":
    raise SystemExit(main())

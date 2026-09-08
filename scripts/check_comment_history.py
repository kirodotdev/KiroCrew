#!/usr/bin/env python3
"""check_comment_history.py -- no change history narrated in comments or docstrings.

## The rule this enforces

``docs/system-specs/common/code-style.md`` ("Comments explain the WHY") already
says a comment must not carry PR or review numbers, ticket ids, commit SHAs,
incident dates, or historical narration -- "previously", "used to", "we now",
"Status: implemented". That history lives in git, and a comment that narrates a
change is stale the moment the next change lands: a reader cannot tell whether it
describes the code in front of them or the code it replaced.

Nothing enforced it, so the tree carries thousands of such lines. That is the
familiar failure of a documented-but-unchecked rule: contributors read the rule,
see the surrounding code break it, and copy the code.

## What counts as a violation

A COMMENT token or a DOCSTRING whose text matches one of ``PATTERNS``. Both are
found with ``tokenize`` and ``ast``, so a string literal that is not a docstring
is never scanned -- a user-facing error message reading "this token is no longer
valid" is behavior, not narration, and flagging it would make the gate wrong in
the one place it is most tempting to write the words.

One violation is one distinct MATCHED SPAN, not one line: a comment naming two
separate markers has narrowed by half when one is removed, and a count that could
not see that would let the ratchet stall. Overlapping hits collapse to the widest
one, so a single ``(#4211)`` that two patterns both recognise counts once -- a
count that moved by two when a reader deleted one reference would read as a
mistake and invite raising the entry instead of trusting it.

Pragma comments (``# type: ignore``, ``# noqa``, ``# pragma``, ``# fmt:``) are
exempt, as code-style.md says, and ``src/kiro_crew/_vendor/`` is excluded because
vendored third-party code is not ours to rewrite.

## The ratchet

Existing violations are recorded in ``comment-history-baseline.json`` as
``path -> count``. The rules mirror ``check_black_formatting.py`` and
``check_subprocess_encoding.py`` -- the repository has solved "large pre-existing
violation set, must only shrink" twice already and a third shape for it would be
a third thing to learn:

* a file NOT in the baseline must be clean;
* a baselined file may not grow its count;
* in a file this change touches, a violation sitting on an ADDED line is a new
  offender even when the count is level -- otherwise deleting one old marker
  while writing a new one would slip through unchanged;
* a baselined file whose count has DROPPED must have its entry lowered (or
  removed at 0) in the same change, so the list only shrinks. Run
  ``--write-baseline``, which only lowers counts and deletes entries.

Like both gates above, the "new offender" and "must lower" verdicts cover only
the files THIS change touches. CI evaluates a PR's merge ref, so an unscoped gate
reddens a PR because the base branch merged someone else's file or someone else's
cleanup -- a colour no contributor could act on.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "comment-history-baseline.json"
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

WRITE_HINT = "python3 scripts/check_comment_history.py --write-baseline"
BASELINE_COMMENT = (
    "Comments and docstrings that narrate change history, per file, as a match "
    "COUNT. The rule they break is in docs/system-specs/common/code-style.md "
    '("Comments explain the WHY"); it predates any enforcement, so the tree is '
    "recorded here rather than rewritten in one pass. The gate requires every "
    "OTHER file to be clean and none of these counts to grow, so this list can "
    "only shrink. Do NOT add or raise an entry to make a red gate green: a new "
    "offender means the comment should state CURRENT behavior in present tense. "
    f"Lower and prune with `{WRITE_HINT}`, which never adds or raises one."
)


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


def _matches(text: str) -> list[tuple[int, str]]:
    """(offset, matched text) for each distinct hit, overlaps collapsed.

    Several patterns describe the same marker on purpose -- a parenthesised issue
    reference is also a bare issue number -- so counting raw hits would report one
    reference as two. A reader who deletes it would then watch the count fall by
    two and reasonably conclude the gate is wrong.

    The OFFSET is returned because a docstring hit has to be reported at its own
    line, not at the docstring's first line: the added-line rule compares against
    the lines a diff touched, and a hit reported on the wrong line is invisible to
    it.
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
    return [(start, matched) for start, _, matched in kept]


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


def violations_in_source(source: str) -> list[tuple[int, str]]:
    """(line, matched text) for every history marker in a comment or docstring.

    Raises ``SyntaxError`` when the source does not parse, and
    ``tokenize.TokenError`` when it does not tokenize: both are hard errors for
    the caller, never "clean". Under a shrink-only ratchet a parse failure read
    as zero violations would invite a prune that deletes the file's real entry.
    """
    found: list[tuple[int, str]] = []

    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT or _is_pragma(token.string):
            continue
        for _, text in _matches(token.string):
            found.append((token.start[0], text))

    for node in _docstring_nodes(ast.parse(source)):
        for offset, text in _matches(node.value):
            # The line the marker is ON, not the docstring's first line. Reporting
            # the opening line would put every hit in a multi-line docstring
            # outside the set of lines the diff added, so the added-line rule --
            # the one that catches swapping a fresh marker in for an old one at a
            # level count -- would never fire inside a docstring.
            #
            # Counted over the PARSED value, so an escaped \n in a single-line
            # docstring shifts the report down a line. Docstrings spell newlines
            # literally, so this is the rare case, and the answer still lands
            # inside the literal being fixed.
            found.append((node.lineno + node.value.count("\n", 0, offset), text))

    return sorted(found)


def _excluded(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in EXCLUDED_DIRS)


def _scan(targets: tuple[str, ...]) -> dict[str, list[tuple[int, str]]]:
    """Map of repo-relative path -> violations, for files with any."""
    results: dict[str, list[tuple[int, str]]] = {}
    for name in targets:
        target = ROOT / name
        if not target.is_dir():
            raise SystemExit(f"target {name} does not exist under {ROOT}")
        for path in sorted(target.rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if _excluded(rel):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if not _ANY_MARKER.search(source):
                continue
            try:
                found = violations_in_source(source)
            except (SyntaxError, tokenize.TokenError) as exc:
                raise SystemExit(
                    f"{rel} does not parse ({exc}); refusing to read a parse "
                    "failure as zero violations -- under a shrink-only ratchet "
                    "that would invite a prune deleting the file's real entry"
                )
            if found:
                results[rel] = found
    return results


def _read_baseline(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise SystemExit(
            f"baseline {path} is missing; restore it from git rather than "
            "regenerating it, since a regenerated baseline would silently absorb "
            "every offender added since it was recorded"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"baseline {path} is not valid JSON: {exc}")
    files = document.get("files")
    if not isinstance(files, dict):
        raise SystemExit(f"baseline {path} has no 'files' object")
    entries: dict[str, int] = {}
    for rel, count in files.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise SystemExit(f"malformed baseline count for {rel}: {count!r}")
        entries[rel] = count
    return entries


def _write_baseline(path: Path, entries: dict[str, int]) -> None:
    document = {
        "_comment": BASELINE_COMMENT,
        "_total": sum(entries.values()),
        "files": {rel: entries[rel] for rel in sorted(entries)},
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _shrunken_baseline(baseline: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    """The refresh result: counts only ever lowered, clean/gone entries dropped."""
    survivors: dict[str, int] = {}
    for rel, recorded in baseline.items():
        now = current.get(rel, 0)
        if now > 0:
            survivors[rel] = min(recorded, now)
    return survivors


def _verdicts(
    violations: dict[str, list[tuple[int, str]]],
    baseline: dict[str, int],
    changed: set[str] | None,
    added: dict[str, set[int]] | None,
) -> tuple[list[str], list[str], dict[str, list[tuple[int, str]]], list[str]]:
    """(new_offenders, grown, added_line_offenders, shrunk) under the ratchet.

    ``changed`` None means scope was undeterminable: judge the whole tree.
    ``added`` None means added-line info was unavailable: skip only that rule.
    """
    current = {rel: len(found) for rel, found in violations.items()}

    def in_scope(rel: str) -> bool:
        return changed is None or rel in changed

    new_offenders: list[str] = []
    grown: list[str] = []
    added_line_offenders: dict[str, list[tuple[int, str]]] = {}
    shrunk: list[str] = []
    for rel, count in sorted(current.items()):
        recorded = baseline.get(rel)
        if recorded is None:
            if in_scope(rel):
                new_offenders.append(rel)
        elif in_scope(rel):
            if count > recorded:
                grown.append(rel)
            elif added is not None:
                on_added = [item for item in violations[rel] if item[0] in added.get(rel, set())]
                if on_added:
                    added_line_offenders[rel] = on_added
    for rel, recorded in sorted(baseline.items()):
        if current.get(rel, 0) < recorded and in_scope(rel):
            shrunk.append(rel)
    return new_offenders, grown, added_line_offenders, shrunk


def _report(rel: str, found: list[tuple[int, str]]) -> None:
    for line, text in found:
        print(f"  {rel}:{line}: {text}")


def _grown_error(
    rel: str,
    recorded: int,
    count: int,
    found: list[tuple[int, str]],
    added: dict[str, set[int]] | None,
) -> str:
    """The grown-count error line, worded by where the matched lines sit.

    A match on one of this diff's added lines keeps the licensing wording:
    the diff places a marker on a line it authors. Zero matches on added
    lines reads as base-branch drift, so the wording must not accuse the
    diff. The judgment is only as good as ``added``: it reflects committed
    added lines, and ``None`` means added-line scope is unavailable, so
    keep the stricter wording rather than assert inherited drift on a
    guess.
    """
    if added is not None:
        added_lines = added.get(rel, set())
        if all(line not in added_lines for line, _ in found):
            return (
                f"::error file={rel}::history narration in comments grew from "
                f"{recorded} to {count} on the base branch; this diff adds none "
                "of the matched lines. Rewording the matched lines is what "
                "clears it. See docs/system-specs/common/code-style.md."
            )
    return (
        f"::error file={rel}::history narration in comments grew from "
        f"{recorded} to {count}. The baseline carries the "
        "existing lines; it does not license new ones."
    )


def run_gate(baseline_path: Path, write: bool) -> int:
    # Baseline first, before the several-thousand-file scan: an absent baseline is
    # a refusal in BOTH modes, and paying for the scan to reach it would make the
    # refusal arrive a minute late.
    baseline = _read_baseline(baseline_path)
    violations = _scan(DEFAULT_TARGETS)
    current = {rel: len(found) for rel, found in violations.items()}

    if write:
        # There is deliberately no "record the tree as it is" path: it would turn
        # `rm` plus one command into a full amnesty for every marker in the tree,
        # which is the one move that would make this gate a formality. The initial
        # record was written once; from here the command only lowers and prunes.
        survivors = _shrunken_baseline(baseline, current)
        pruned = len(baseline) - len(survivors)
        lowered = sum(1 for rel in survivors if survivors[rel] < baseline[rel])
        _write_baseline(baseline_path, survivors)
        print(f"pruned {pruned} entr(y/ies), lowered {lowered}; {len(survivors)} remain")
        return 0

    scope = _load_scope()
    changed, scope_label = scope.changed_paths()
    print(f"comment-history gate scope: {scope_label}", end="")
    print("" if changed is None else f" ({len(changed)} changed file(s))")
    added = scope.added_lines(scope_label) if changed is not None else None

    new_offenders, grown, added_line_offenders, shrunk = _verdicts(
        violations, baseline, changed, added
    )

    for rel in new_offenders:
        print(
            f"::error file={rel}::comment or docstring narrates change history "
            "(PR/issue number, review round, commit SHA, or a phrase like "
            '"previously" / "no longer" / "we now"). State CURRENT behavior in '
            "present tense; the history is in git. See "
            "docs/system-specs/common/code-style.md."
        )
        _report(rel, violations[rel])
    for rel in grown:
        print(_grown_error(rel, baseline[rel], current[rel], violations[rel], added))
        _report(rel, violations[rel])
    for rel, found in added_line_offenders.items():
        print(
            f"::error file={rel}::this change ADDS history narration to a comment "
            "or docstring (the baseline carries only pre-existing lines)."
        )
        _report(rel, found)
    if shrunk:
        print(
            f"::error::{len(shrunk)} baselined file(s) now carry fewer history "
            "markers. Record the progress so the baseline keeps shrinking: "
            f"{WRITE_HINT}"
        )
        for rel in shrunk:
            print(f"  {rel}: {baseline[rel]} -> {current.get(rel, 0)}")

    if new_offenders or grown or added_line_offenders or shrunk:
        print(
            f"\ncomment-history gate FAILED: {len(new_offenders)} new offender(s), "
            f"{len(grown)} grown count(s), {len(added_line_offenders)} file(s) with "
            f"markers on added lines, {len(shrunk)} entr(y/ies) to lower."
        )
        return 1

    total = sum(baseline.values())
    print(
        "comment-history gate passed: nothing in scope narrates change history "
        f"outside the baseline ({total} known marker(s) in {len(baseline)} file(s) "
        "still listed)."
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
    # Two distinct markers count twice; the same marker matched by two patterns
    # counts once. Both halves are what makes a baseline delta readable.
    if len(violations_in_source("p = 1  # previously, see PR #812\n")) != 2:
        failures.append("two distinct markers in one comment must count 2")
    if len(violations_in_source("q = 1  # widen the timeout (#4211)\n")) != 1:
        failures.append("one marker matched by two patterns must count 1")
    # A marker deep in a docstring must report ITS line, or the added-line rule
    # cannot see it.
    interior = '"""Head.\n\nTail: previously it blocked.\n"""\n'
    if violations_in_source(interior) != [(3, "previously")]:
        failures.append("a docstring marker must report the line it sits on")
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
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="lower counts / prune entries that improved; never adds or raises",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="run the rule-family self-test instead of the gate",
    )
    args = parser.parse_args(argv)
    if args.test:
        return _self_test()
    return run_gate(args.baseline, args.write_baseline)


if __name__ == "__main__":
    raise SystemExit(main())

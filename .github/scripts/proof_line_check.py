#!/usr/bin/env python3
"""PR Hygiene: a `fix:` pull request says whether its tests fail without the fix.

A test that still passes with the bug put back does not test the fix. `prove.py`
(in the kirocrew-prepare-pr skill) reverts the production hunks in a throwaway
worktree and re-runs the changed tests. This check asks the author to paste the
result as ONE line under the existing `## Tests` heading (no new section):

    Proof: PROVEN <the tests that fail without the fix> (head <sha12>)
    Proof: not provable -- <reason>

`<reason>` comes from a closed list, so a weekly report can count them:

    frontend test (prove.py runs Python only)
    test-only change
    copy-only change
    docs or config only
    prove.py timed out
    other: <20+ characters>

CI checks, for titles starting `fix` (a `revert` is out of scope):

1. `## Tests` exists (outside fences and HTML comments);
2. it holds exactly one `Proof:` line, and no `Proof:` line sits outside it;
3. the line has one of the two shapes above, with a reason from the list.

Locally, `diff_signals.py --check-body` also sets PROVE_RECORD and TREE_ID. When the
record is for the same tree and holds a line, the submitted line must BE that line, so a
`PROVEN` record cannot be answered with `not provable -- test-only change`; a `PROVEN`
line with no matching record fails; and a record of NOT_PROVEN, INCONCLUSIVE or
BASELINE_RED fails any line. CI cannot do that (the record lives in the author's git
directory), so a hand-typed line passes CI, as `Reader:` lines do.

A line may carry one list marker (`-`, `*`, `+`, `1.`, `1)`), sit in one pair of
backticks, use an em dash for `--`, and end with a period; tabs and CRLF are fine.

LIMIT, stated plainly: this checks that the line is present, well formed and in
the right section. It does not run the tests. A control test that passes both
ways stays valid: the rule asks for one failing test, not all.

Environment (read only as data, never interpolated into code):
    PR_TITLE        the pull request title
    PR_BODY         the pull request body
    PR_CREATED_AT   optional ISO-8601 time the pull request was opened
    PROVE_RECORD    optional path of prove.py's record (local runs)
    TREE_ID         optional tree id of HEAD, compared with the record's
    GITHUB_ACTIONS  when "true", findings print as workflow annotations

Mode: WARN until ENFORCE_FROM is set. A finding prints a warning and exits 0. Once
ENFORCE_FROM holds a date (YYYY-MM-DD), a pull request opened on or after it exits
1 on a finding; one opened before it keeps warning.

Exit: 0 pass, or findings in warn mode | 1 findings in enforce mode | 2 environment error
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timezone

# None = warn mode. Set to "YYYY-MM-DD" to turn the check red for pull requests
# opened on or after that date (RFC: review loop and escaped-bug reduction, D4).
ENFORCE_FROM: str | None = None

TITLE_RE = re.compile(r"^fix([(!:]|$)", re.IGNORECASE)

# The body reader below is the one `pr-description-check.sh` (PR Hygiene's template
# check) and `sibling_list_check.py` use: CR removed, <=3 spaces of indent, a fence
# that closes only on its own character at >= the opening length, an HTML comment
# block that opens on a line starting `<!--`, and a heading matched as a
# case-insensitive prefix of its lowercased line. So "has a `## Tests` heading"
# means the same thing here as in the template check. Keep the copies identical;
# test_proof_line_check.py runs `pr-description-check.sh` over the same bodies.
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t].*)?$")
TESTS_NEEDLE = "## tests"
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMMENT_OPEN_RE = re.compile(r"^ {0,3}<!--")

# One list marker: "-", "*", "+", "1." or "1)". A quoted line ("> ...") is prose.
MARKER_RE = re.compile(r"^(?:[-*+]|\d{1,9}[.)])[ \t]+")
DASH_RE = re.compile(r" (?:\u2014|\u2013) ")
PROOF_PREFIX_RE = re.compile(r"Proof *:", re.IGNORECASE)
PROVEN_RE = re.compile(r"Proof: +(?i:PROVEN) +(?P<tests>\S.*?) +\(head (?P<sha>[0-9a-f]{12})\)\.?$")
NOT_PROVABLE_RE = re.compile(r"Proof: +(?i:not provable) +-- +(?P<reason>\S.*)$")

# The closed list of reasons (RFC Appendix D); `other:` takes 20+ characters.
REASONS = (
    "frontend test (prove.py runs Python only)",
    "test-only change",
    "copy-only change",
    "docs or config only",
    "prove.py timed out",
)
OTHER_RE = re.compile(r"other: (?P<why>\S.*)$")
OTHER_MIN = 20
# Verdicts after which no `Proof:` line is honest: the tests do not catch the bug yet.
UNPROVEN_VERDICTS = ("NOT_PROVEN", "INCONCLUSIVE", "BASELINE_RED")

SHAPES = (
    "'Proof: PROVEN <the tests that fail without the fix> (head <sha12>)' or "
    "'Proof: not provable -- <reason>'"
)


def _cut_comments(line: str) -> str:
    """``line`` without each ``<!-- ... -->`` that opens and closes inside it.

    Plain string search, not a regular expression: this only tidies a visible line (a
    comment that spans lines is handled by ``visible_lines``), and an unclosed ``<!--``
    stays as text, as the template check reads it.
    """
    out: list[str] = []
    rest = line
    while True:
        start = rest.find("<!--")
        end = rest.find("-->", start + 4) if start >= 0 else -1
        if end < 0:
            return "".join(out) + rest
        out.append(rest[:start])
        rest = rest[end + 3 :]


def visible_lines(text: str) -> list[str]:
    """Lines of ``text`` outside fenced blocks and HTML comments, as the template check reads them.

    A comment block opens on a line starting ``<!--`` and ends on the first line holding
    ``-->`` (that whole line is hidden); a comment that opens and closes inside a line is
    cut out of it. A fence marker inside a comment, and a comment inside a fence, both
    stay hidden. An unclosed fence or comment hides the rest.
    """
    kept: list[str] = []
    fence: tuple[str, int] | None = None  # (character, opening length)
    in_comment = False
    for raw in text.replace("\r", "").split("\n"):
        if in_comment:
            in_comment = "-->" not in raw
            continue
        m = FENCE_RE.match(raw)
        if fence is not None:
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= fence[1]
                and not raw.strip().strip(fence[0])
            ):
                fence = None
            continue
        if m:
            fence = (m.group(1)[0], len(m.group(1)))
            continue
        if COMMENT_OPEN_RE.match(raw):
            in_comment = "-->" not in raw
            continue
        kept.append(_cut_comments(raw))
    return kept


def proof_text(line: str) -> str:
    """A body line as the Proof shapes read it.

    One list marker and one pair of backticks around the whole line are removed; tabs and
    runs of spaces become one space; an em or en dash between spaces becomes ``--``.
    """
    text = MARKER_RE.sub("", line.strip(), count=1).strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        text = text[1:-1].strip()
    return DASH_RE.sub(" -- ", re.sub(r"\s+", " ", text))


def split_section(body: str, needle: str) -> tuple[list[str] | None, list[str]]:
    """(lines of the first level-2 section whose heading starts with ``needle``, other lines).

    ``needle`` is lowercase and starts with ``## ``, like the template check's
    ``REQUIRED_SECTIONS``: ``"## tests"``. The first element is None when no heading
    matches. A heading of level 1 or 2 ends the section; a deeper one stays inside it.
    Every line is stripped.
    """
    inside: list[str] | None = None
    outside: list[str] = []
    in_section = False
    for line in visible_lines(body):
        h = HEADING_RE.match(line)
        if in_section and h and len(h.group(1)) <= 2:
            in_section = False
        if h and inside is None and line.lstrip(" ").lower().startswith(needle):
            in_section = True
            inside = []
            continue
        (inside if in_section and inside is not None else outside).append(line.strip())
    return inside, outside


def split_sections(body: str) -> tuple[list[str] | None, list[str]]:
    """(lines of the `## Tests` section or None, every other visible line)."""
    return split_section(body, TESTS_NEEDLE)


def is_in_scope(title: str) -> bool:
    return bool(TITLE_RE.match(title.strip()))


def reason_ok(reason: str) -> bool:
    reason = reason.strip()
    reason = reason.rstrip(".").strip()
    if reason.lower() in {r.lower() for r in REASONS}:
        return True
    other = OTHER_RE.match(reason)
    return bool(other and len(other.group("why").strip()) >= OTHER_MIN)


def check(title: str, body: str) -> tuple[list[str], re.Match | None]:
    """Findings for one pull request, and the parsed Proof line if it has a valid shape.

    The match is of ``PROVEN_RE`` or ``NOT_PROVABLE_RE``; its ``string`` is the line
    as ``proof_text`` reads it.
    """
    if not is_in_scope(title):
        return [], None
    inside, outside = split_sections(body)
    if inside is None:
        return [
            "fix pull requests must carry a '## Tests' section holding one Proof line: " + SHAPES
        ], None
    problems: list[str] = []
    proofs = [ln for ln in inside if PROOF_PREFIX_RE.match(proof_text(ln))]
    stray = [ln for ln in outside if PROOF_PREFIX_RE.match(proof_text(ln))]
    for line in stray:
        problems.append(f"'{line}' is outside '## Tests': move the Proof line under that heading")
    if not proofs:
        if not stray:
            problems.append("'## Tests' has no Proof line: add " + SHAPES)
        return problems, None
    if len(proofs) > 1:
        problems.append(f"'## Tests' has {len(proofs)} Proof lines; keep one")
    line = proof_text(proofs[0])
    proven = PROVEN_RE.match(line)
    if proven:
        return problems, proven
    not_provable = NOT_PROVABLE_RE.match(line)
    if not_provable:
        if not reason_ok(not_provable.group("reason")):
            problems.append(
                f"'{line}': the reason must be one of: " + "; ".join(REASONS) + "; "
                f"other: <{OTHER_MIN}+ characters>"
            )
        return problems, not_provable
    problems.append(f"'{line}' does not parse: write " + SHAPES)
    return problems, None


def comparable(line: str) -> str:
    """A Proof line as it is compared with the recorded one: shape-normalised, no final period."""
    text = proof_text(line)
    return text[:-1] if text.endswith(".") else text


def compare_record(match: re.Match, record_path: str, tree: str) -> list[str]:
    """Findings from comparing the Proof line with prove.py's record for this tree.

    When the record is for this tree and holds a line, the submitted line must be that
    line: a `PROVEN` record cannot be answered with `not provable`, and the other way.
    A `PROVEN` line with no usable record fails; any other line without one is CI's
    shape-only case and passes.
    """
    proven_line = match.re is PROVEN_RE
    try:
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        if not proven_line:
            return []
        return [
            f"the PROVEN line has no prove.py record ({record_path}): run prove.py and paste "
            "the line it prints"
        ]
    if not isinstance(record, dict):
        return ["the prove.py record is not a JSON object: re-run prove.py"]
    if record.get("tree") != tree:
        if not proven_line:
            return []
        return [
            "the prove.py record is for a different tree (the code changed since): re-run "
            "prove.py and paste the line it prints"
        ]
    expected = record.get("line")
    if isinstance(expected, str) and expected.strip():
        if comparable(expected) == comparable(match.string):
            return []
        return [
            f"prove.py recorded '{expected}' for this tree, and the Proof line says "
            f"'{match.string}': paste the line prove.py prints"
        ]
    verdict = record.get("verdict")
    if verdict in UNPROVEN_VERDICTS:
        return [
            f"prove.py recorded {verdict} for this tree: the tests do not fail without the "
            "fix, and no Proof line says otherwise. Fix the tests and re-run prove.py"
        ]
    if not proven_line:
        return []
    if verdict != "PROVEN":
        return [
            f"prove.py recorded {verdict} for this tree, not PROVEN: the tests do not fail "
            "without the fix"
        ]
    problems: list[str] = []
    head = str(record.get("head", ""))[:12]
    if match.group("sha") != head:
        problems.append(
            f"the line names head {match.group('sha')}, prove.py recorded {head or 'none'}"
        )
    remainder = match.group("tests").replace("<&#33;--", "<!--")  # prove.py's escape
    tests = [t for t in record.get("tests", []) if isinstance(t, str) and t]
    named = 0
    for test in sorted(tests, key=len, reverse=True):
        if test in remainder:
            named += 1
            remainder = remainder.replace(test, "")
    if not named or remainder.strip(" ,;\t"):
        problems.append(
            "the line names tests the prove.py record does not list: paste the line it prints"
        )
    return problems


def enforced(created_at: str | None, enforce_from: str | None = None) -> bool:
    """Whether a pull request opened at ``created_at`` is held to the check.

    A pull request with no known opening time counts as opened now.
    """
    cutoff = enforce_from if enforce_from is not None else ENFORCE_FROM
    if not cutoff:
        return False
    start = date.fromisoformat(cutoff)
    if created_at:
        text = created_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        opened = datetime.fromisoformat(text).date()
    else:
        opened = datetime.now(timezone.utc).date()
    return opened >= start


def main() -> int:
    title = os.environ.get("PR_TITLE", "")
    body = os.environ.get("PR_BODY", "")
    if not is_in_scope(title):
        print("Not a fix pull request; Proof line not required.")
        return 0
    problems, parsed = check(title, body)
    record_path = os.environ.get("PROVE_RECORD", "")
    tree = os.environ.get("TREE_ID", "")
    if parsed is not None and record_path and tree:
        problems += compare_record(parsed, record_path, tree)
    if not problems:
        print("Proof line present.")
        return 0
    try:
        red = enforced(os.environ.get("PR_CREATED_AT"))
    except ValueError as exc:
        print(f"proof_line_check: bad date ({exc})", file=sys.stderr)
        return 2
    level = "error" if red else "warning"
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    for problem in problems:
        if in_ci:
            print(f"::{level}::Proof line: {problem}")
        else:
            print(f"PROOF LINE ({level}): {problem}")
    if not red:
        print("The Proof line is advisory for now: this does not fail the check.")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())

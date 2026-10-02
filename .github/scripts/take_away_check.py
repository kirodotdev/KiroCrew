#!/usr/bin/env python3
"""PR Hygiene: a diff that takes something away must list its readers.

A "take-away" diff hides, deletes, tightens or migrates something main
currently has. CI rarely catches the break: the reader that breaks sits on
another entry point than the one the author tested. The worked examples are
in docs/system-specs/common/take-away-changes.md.

Shapes, kept small on purpose: a `*_migration.py` file, a new
`extra_hidden_dirs=` argument, a `config.agents` row `del`/`.pop`, and a
removed module-level public `def`. Test files never count.

Usage:
    take_away_check.py DIFF_FILE

DIFF_FILE is the PR's merge-base diff (`git diff <merge-base> <head>`). The
PR body comes from the PR_BODY environment variable and is only ever read as
data, never interpolated into code.

When the diff matches a take-away shape, the body's `## Backwards
compatibility` section must carry at least one line of the form

    Reader: <path>:<symbol> -- <entry> -- <why it still works | test name>

where <entry> is one of chat, cron, subagent, app, crew page, release.
`Removes nothing: <why>` does NOT satisfy a hit: the diff says otherwise.

LIMIT, stated plainly: this checks that reader lines are present and well
formed, not that the list is complete or true. Judging that is the
reviewers' job.

Exit 0 = pass (no hit, or a hit with reader lines); 1 = fail.
"""

from __future__ import annotations

import os
import re
import sys
from collections import Counter

ENTRIES = ("chat", "cron", "subagent", "app", "crew page", "release")

# The canonical reader line. Design Review parses the same format, so change
# both or neither. An optional list bullet is accepted.
READER_RE = re.compile(
    r"^(?:[-*] +)?Reader: +(?P<path>[^\s:]+):(?P<symbol>\S+) +-- +"
    r"(?P<entry>" + "|".join(re.escape(e) for e in ENTRIES) + r") +-- +(?P<why>\S.*)$"
)
READER_PREFIX_RE = re.compile(r"^(?:[-*] +)?Reader *:", re.IGNORECASE)
SECTION_RE = re.compile(r"^ {0,3}##+ +Backwards compatibility\b", re.IGNORECASE)
HEADING_RE = re.compile(r"^ {0,3}#{1,6}( |$)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

# Shapes matched on a single changed line. Each is compared as a multiset of
# normalised added vs removed lines per file, so a pure re-wrap or move inside
# one file does not count.
# An exact pass-through (`extra_hidden_dirs=extra_hidden_dirs`) forwards a
# mask, it does not add one, so it is excluded; an expression built on the
# forwarded value is not.
HIDDEN_DIR_RE = re.compile(
    r"\bextra_hidden_dirs\s*=\s*(?!None\b|extra_hidden_dirs\s*(?:[,)]|$))"
    r"(?:\([^)]*\)?|\[[^\]]*\]?|[^\s,)]+)"
)
AGENTS_ROW_DELETE_RE = re.compile(
    r"\bdel\s+[\w.]*\bagents\s*\[[^\]]*\]?|\b[\w.]*\bagents\s*\.\s*pop\s*\([^)]*\)?"
)
# Column 0 only: a module-level function. An indented `def` in a removed line
# may be a method or a docstring example, which a line diff cannot tell apart.
PUBLIC_DEF_RE = re.compile(r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(")


class FileDiff:
    """One file's changed lines. A plain class: the tests load this script by
    path, where a dataclass cannot resolve its own module."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.added: list[str] = []
        self.removed: list[str] = []


def parse_diff(text: str) -> list[FileDiff]:
    """Split a unified git diff into per-file added/removed lines.

    Does not rely on hunk line counts, so a hand-trimmed fixture still parses.
    """
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            cur = FileDiff(path=m.group(2) if m else line.split()[-1])
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if not in_hunk:
            if line.startswith("+++ b/"):
                cur.path = line[6:]
            in_hunk = line.startswith("@@")
            continue
        if line.startswith("+"):
            cur.added.append(line[1:])
        elif line.startswith("-"):
            cur.removed.append(line[1:])
    return files


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        path.startswith(("test/", "tests/"))
        or "/test/" in path
        or "/tests/" in path
        or "/__tests__/" in path
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def _norm(line: str) -> str:
    return " ".join(line.split()).rstrip(",")


def _net_added(fd: FileDiff, pattern: re.Pattern[str]) -> list[str]:
    """Matches on added lines that no removed line in the file also carries.

    Compared on the matched text, not the whole line, so re-wrapping a call
    (`f(a, x=y)` -> `f(\\n a,\\n x=y,\\n)`) is not a new occurrence.
    """
    added = Counter(_norm(m.group(0)) for x in fd.added for m in pattern.finditer(x))
    removed = Counter(_norm(m.group(0)) for x in fd.removed for m in pattern.finditer(x))
    return sorted((added - removed).elements())


PRODUCT_DIR = "src/"


def _is_code(line: str) -> bool:
    """A changed line that is neither blank nor a ``#`` comment."""
    s = line.strip()
    return bool(s) and not s.startswith("#")


def detect(diff_text: str) -> list[str]:
    """Return one human-readable hit per take-away shape found."""
    files = parse_diff(diff_text)
    hits: list[str] = []
    # (path, name): a def moved to another module still breaks its old
    # import path, so only a re-add in the SAME file cancels a removal.
    added_defs: set[tuple[str, str]] = set()
    for fd in files:
        if fd.path.endswith(".py") and not _is_test_path(fd.path):
            for x in fd.added:
                m = PUBLIC_DEF_RE.match(x)
                if m:
                    added_defs.add((fd.path, m.group(1)))

    for fd in files:
        # Product code only: a reader of CI tooling or docs is not a user.
        if not fd.path.startswith(PRODUCT_DIR) or _is_test_path(fd.path):
            continue
        name = fd.path.rsplit("/", 1)[-1]
        if name.endswith("_migration.py") and any(_is_code(x) for x in fd.added + fd.removed):
            hits.append(f"migration: {fd.path} added or changed")
        if not fd.path.endswith(".py"):
            continue
        for x in _net_added(fd, HIDDEN_DIR_RE):
            hits.append(f"hide: {fd.path} passes a new extra_hidden_dirs ({x})")
        for x in _net_added(fd, AGENTS_ROW_DELETE_RE):
            hits.append(f"delete: {fd.path} deletes config.agents rows ({x})")
        for x in fd.removed:
            m = PUBLIC_DEF_RE.match(x)
            if m and (fd.path, m.group(1)) not in added_defs:
                hits.append(f"remove: {fd.path} removes public def {m.group(1)}()")
    return hits


def compat_section(body: str) -> list[str] | None:
    """Lines of `## Backwards compatibility`, outside comments and fences.

    None when the section is absent.
    """
    lines = body.replace("\r", "").split("\n")
    out: list[str] | None = None
    fence: str | None = None
    in_comment = False
    for raw in lines:
        if fence is not None:
            stripped = raw.strip()
            if stripped.startswith(fence[0] * len(fence)) and set(stripped) <= {fence[0]}:
                fence = None
            continue
        if in_comment:
            if "-->" in raw:
                in_comment = False
                raw = raw.split("-->", 1)[1]
            else:
                continue
        # Drop inline/opening comments on this line.
        while "<!--" in raw:
            before, _, after = raw.partition("<!--")
            if "-->" in after:
                raw = before + after.split("-->", 1)[1]
            else:
                raw = before
                in_comment = True
                break
        m = FENCE_RE.match(raw)
        if m:
            fence = m.group(1)
            continue
        if HEADING_RE.match(raw):
            if out is not None:
                break
            if SECTION_RE.match(raw):
                out = []
            continue
        if out is not None:
            out.append(raw.strip())
    return out


def check(diff_text: str, body: str) -> tuple[bool, str]:
    hits = detect(diff_text)
    if not hits:
        return True, "No take-away shape in the diff; reader list not required."
    shown = "\n".join(f"  - {h}" for h in hits[:20])
    if len(hits) > 20:
        shown += f"\n  - ... and {len(hits) - 20} more"
    head = f"This diff takes something away:\n{shown}\n"
    fmt = (
        "\nList every reader of what it takes away under '## Backwards compatibility',"
        " one line each, found by grepping origin/main across every entry point"
        " (chat, cron, subagent, app bundles, crew page, release branch):\n\n"
        "  Reader: <path>:<symbol> -- <chat|cron|subagent|app|crew page|release>"
        " -- <why it still works | test name>\n\n"
        "'Removes nothing:' does not satisfy this check. See"
        " docs/system-specs/common/take-away-changes.md."
    )
    section = compat_section(body)
    if section is None:
        return False, head + "\nThe PR body has no '## Backwards compatibility' section." + fmt
    readers = [x for x in section if READER_RE.match(x)]
    if readers:
        return True, head + f"\n{len(readers)} Reader: line(s) present."
    bad = [x for x in section if READER_PREFIX_RE.match(x)]
    msg = head + "\n'## Backwards compatibility' has no well-formed 'Reader:' line."
    if bad:
        msg += "\nMalformed:\n" + "\n".join(f"  {x}" for x in bad[:10])
    return False, msg + fmt


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: take_away_check.py DIFF_FILE", file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8", errors="replace") as fh:
        diff_text = fh.read()
    ok, msg = check(diff_text, os.environ.get("PR_BODY", ""))
    if ok:
        print(msg)
        return 0
    # The annotation is the line an agent reads first, so it names the fix.
    # Built, not literal: see the note above the pr-hygiene job.
    print(
        "::%s::%s"
        % (
            "error",
            "This diff takes something away: list each reader as"
            " 'Reader: <path>:<symbol> -- <entry> -- <why>' under"
            " '## Backwards compatibility'.",
        )
    )
    print(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
